"""Rotate the artwork on a Samsung Frame TV.

Run this every minute (cron, a systemd timer, or launchd). It reads
rotation.json from the data directory and, when a change is due, shows
the next piece from the slideshow pool: a random piece in "shuffle"
mode, or the next one in "ordered" mode. It only acts while the TV is
in Art Mode, and it throttles itself to the configured interval, so
calling it every minute is safe.

Configuration matches frame_app.py: the data directory is
$FRAME_DATA_DIR or ~/.frame-artwork, and config.json there holds the
TV address (see config.example.json).
"""
import asyncio, json, os, random, sys
from datetime import datetime

DATA_DIR = os.path.expanduser(os.environ.get("FRAME_DATA_DIR",
                                             "~/.frame-artwork"))

def load_config():
    cfg = {"tv_host": None, "tv_port": 8002}
    try:
        with open(os.path.join(DATA_DIR, "config.json")) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if os.environ.get("FRAME_TV_HOST"):
        cfg["tv_host"] = os.environ["FRAME_TV_HOST"]
    return cfg

CFG = load_config()
TOKEN = os.path.join(DATA_DIR, "tv_token.txt")
ROT = os.path.join(DATA_DIR, "rotation.json")

def load_rotation():
    try:
        with open(ROT) as f:
            return json.load(f)
    except Exception:
        return {"enabled": False, "interval_min": 10}

def save_rotation(rot):
    with open(ROT, "w") as f:
        json.dump(rot, f, indent=1)

async def tick(force=False):
    if not CFG["tv_host"]:
        return "not configured (set tv_host in config.json)"
    rot = load_rotation()
    if not force:
        if not rot.get("enabled"):
            return "disabled"
        try:
            interval = max(1, min(60, int(rot.get("interval_min", 10))))
        except Exception:
            interval = 10
        last = rot.get("last_run")
        if last:
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
                if elapsed < interval * 60:
                    return "not due"
            except Exception:
                pass
    from samsungframetv.async_art import SamsungTVAsyncArt
    art = SamsungTVAsyncArt(host=CFG["tv_host"],
                            port=int(CFG["tv_port"]), token_file=TOKEN,
                            name="Frame Artwork Rotation", timeout=60)
    try:
        await asyncio.wait_for(art.open(), 70)
        if await art.get_artmode() != "on":
            return "not in art mode, skipped"
        cur = await art.get_current()
        cur_id = cur.get("content_id") if isinstance(cur, dict) else None
        items = await art.available(category="MY-C0002")
        live = [i["content_id"] for i in items]
        pool = rot.get("pool")
        if not isinstance(pool, list):
            pool = list(live)
            rot["pool"] = pool
            save_rotation(rot)
        ordered = [c for c in pool if c in set(live)]
        if rot.get("mode") == "ordered":
            if not ordered:
                return "empty pool"
            if cur_id in ordered:
                pick = ordered[(ordered.index(cur_id) + 1) % len(ordered)]
            elif rot.get("last_id") in ordered:
                pick = ordered[(ordered.index(rot["last_id"]) + 1) % len(ordered)]
            else:
                pick = ordered[0]
            if pick == cur_id:
                return "only one piece in pool"
        else:
            candidates = [c for c in ordered if c != cur_id]
            if not candidates:
                return "empty pool"
            pick = random.choice(candidates)
        await art.select_image(pick, show=True)
        after = await art.get_current()
        shown = after.get("content_id") if isinstance(after, dict) else None
        rot["last_id"] = shown
        rot["last_run"] = datetime.now().isoformat(timespec="seconds")
        save_rotation(rot)
        return "rotated %s -> %s" % (cur_id, shown)
    finally:
        try:
            await art.close()
        except Exception:
            pass

force = len(sys.argv) > 1 and sys.argv[1] == "force"
try:
    result = asyncio.run(tick(force))
    if result not in ("not due", "disabled"):
        print(datetime.now().isoformat(timespec="seconds"), result, flush=True)
except Exception as e:
    print(datetime.now().isoformat(timespec="seconds"), "tick failed:", e, flush=True)
