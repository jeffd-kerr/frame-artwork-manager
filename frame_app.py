import asyncio, json, os, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# --- Configuration -------------------------------------------------------
# Everything the app keeps (labels, thumbnails, uploads, rotation state)
# lives in one data directory: $FRAME_DATA_DIR, or ~/.frame-artwork.
# The TV address and ports come from config.json in that directory (see
# config.example.json); FRAME_TV_HOST overrides the TV address.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.expanduser(os.environ.get("FRAME_DATA_DIR",
                                             "~/.frame-artwork"))

def load_config():
    cfg = {"tv_host": None, "tv_port": 8002, "port": 8099,
           "bind": "0.0.0.0"}
    try:
        with open(os.path.join(DATA_DIR, "config.json")) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if os.environ.get("FRAME_TV_HOST"):
        cfg["tv_host"] = os.environ["FRAME_TV_HOST"]
    return cfg

CFG = load_config()
HOST = CFG["tv_host"]
TV_PORT = int(CFG["tv_port"])
TOKEN = os.path.join(DATA_DIR, "tv_token.txt")
ROT_PATH = os.path.join(DATA_DIR, "rotation.json")
ROTATE_SCRIPT = os.path.join(APP_DIR, "frame_rotate.py")
LABELS_PATH = os.path.join(DATA_DIR, "labels.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
UPLOADED_PATH = os.path.join(DATA_DIR, "uploaded.json")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
PAGE_PATH = os.path.join(APP_DIR, "page.html")
PORT = int(CFG["port"])

tv_lock = threading.Lock()
SNAP_PATH = os.path.join(DATA_DIR, "last_snapshot.json")
_cache = {"ts": 0, "snap": None, "live": False}
CACHE_TTL = 45
_refreshing = {"on": False}

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)

_persisted = load_json(SNAP_PATH, None)
if _persisted:
    _cache["snap"] = _persisted

def backup_ids():
    # Only pieces uploaded through this app have a local copy; anything
    # added to the TV another way exists only on the TV itself.
    return set(load_json(UPLOADED_PATH, {}).keys())

CANVAS = (3840, 2160)
# Mat window: an even physical margin on all four sides, matched to the
# ~1 inch mat bands Samsung's own upload flow produces (~102 px/in at 4K).
MAT_MARGIN = 105
WIN_W, WIN_H = CANVAS[0] - 2 * MAT_MARGIN, CANVAS[1] - 2 * MAT_MARGIN
WIN_X, WIN_Y = MAT_MARGIN, MAT_MARGIN
KEYLINE = 14                       # classic mat white border, px at 4K

def _crop_rect(img, crop, aw=16, ah=9):
    W, H = img.size
    if crop:
        fx, fy, fw, fh = crop
        x0 = max(0, min(W - 1, round(fx * W)))
        y0 = max(0, min(H - 1, round(fy * H)))
        x1 = max(x0 + 1, min(W, round((fx + fw) * W)))
        y1 = max(y0 + 1, min(H, round((fy + fh) * H)))
        return (x0, y0, x1, y1)
    # default: centered crop to the target aspect
    if W * ah >= H * aw:
        w = H * aw // ah
        return ((W - w) // 2, 0, (W - w) // 2 + w, H)
    h = W * ah // aw
    return (0, (H - h) // 2, W, (H - h) // 2 + h)

def _paste_shadow(canvas_rgba, win, cx, cy):
    from PIL import Image, ImageFilter
    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    sil = Image.new("RGBA", win.size, (0, 0, 0, 150))
    layer.alpha_composite(sil, (cx, cy + 20))
    layer = layer.filter(ImageFilter.GaussianBlur(20))
    out = Image.alpha_composite(canvas_rgba, layer)
    out.alpha_composite(win.convert("RGBA"), (cx, cy))
    return out

def frame_and_save(data, style, stem, color=(250, 248, 242), crop=None):
    import io, re
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        bg = Image.new("RGB", img.size, (250, 248, 242))
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
    if style == "fill":
        out = img.crop(_crop_rect(img, crop)).resize(CANVAS)
    elif style in ("classic", "modern"):
        win = img.crop(_crop_rect(img, crop, WIN_W, WIN_H)).resize((WIN_W, WIN_H))
        base = Image.new("RGBA", CANVAS, color + (255,))
        if style == "classic":
            board = Image.new("RGBA", (WIN_W + 2 * KEYLINE, WIN_H + 2 * KEYLINE),
                              (255, 255, 255, 255))
            base.alpha_composite(board, (WIN_X - KEYLINE, WIN_Y - KEYLINE))
            base.alpha_composite(win.convert("RGBA"), (WIN_X, WIN_Y))
            out = base.convert("RGB")
        else:
            out = _paste_shadow(base, win, WIN_X, WIN_Y).convert("RGB")
    else:  # original: native aspect, floating on the mat with a shadow
        t = img.copy()
        t.thumbnail((int(CANVAS[0] * 0.88), int(CANVAS[1] * 0.88)))
        base = Image.new("RGBA", CANVAS, color + (255,))
        cx, cy = (CANVAS[0] - t.width) // 2, (CANVAS[1] - t.height) // 2
        out = _paste_shadow(base, t, cx, cy).convert("RGB")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:60] or "photo"
    path = os.path.join(UPLOAD_DIR, "%d_%s.jpg" % (int(time.time()), safe))
    out.save(path, quality=90)
    return path

def load_labels():
    labels = load_json(LABELS_PATH, None)
    if labels is None:
        labels = {}
        save_json(LABELS_PATH, labels)
    return labels

async def _snapshot_inner():
    from samsungframetv.async_art import SamsungTVAsyncArt
    art = SamsungTVAsyncArt(host=HOST, port=TV_PORT, token_file=TOKEN,
                            name="Frame Artwork Manager", timeout=20)
    try:
        await asyncio.wait_for(art.open(), 12)
        items = await art.available(category="MY-C0002")
        seen, uploads = set(), []
        for i in items:
            cid = i["content_id"]
            if cid not in seen:
                seen.add(cid)
                uploads.append(cid)
        cur = await art.get_current()
        cur_id = cur.get("content_id") if isinstance(cur, dict) else None
        mode = await art.get_artmode()
        return {"uploads": uploads, "current_id": cur_id, "artmode": mode}
    finally:
        try:
            await art.close()
        except Exception:
            pass

async def _snapshot():
    return await asyncio.wait_for(_snapshot_inner(), 20)

def _run_helper(argv, timeout):
    """Run one TV operation in a fresh helper process (this same file).

    The TV library's connections degrade over a long-lived process's
    lifetime: after a process has made several connections, new ones
    start failing with EHOSTUNREACH ("No route to host") even though
    the TV is up and fresh processes connect instantly. Isolating
    every operation in a clean process makes the app immune to that,
    at the cost of a fraction of a second per operation. (The /api/next
    handler already used this pattern via frame_rotate.py.)
    """
    env = dict(os.environ)
    env["FRAME_DATA_DIR"] = DATA_DIR
    p = subprocess.run([sys.executable, os.path.abspath(__file__)] + argv,
                       capture_output=True, text=True, timeout=timeout,
                       env=env)
    lines = (p.stdout or "").strip().splitlines()
    if not lines:
        raise RuntimeError("TV helper failed (exit %s): %s"
                           % (p.returncode, (p.stderr or "")[-300:]))
    res = json.loads(lines[-1])
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "TV operation failed")
    return res.get("result")

def _attempt_snapshot():
    for i in range(2):
        try:
            with tv_lock:
                snap = _run_helper(["--snapshot"], timeout=60)
            _cache["snap"], _cache["ts"], _cache["live"] = \
                snap, time.time(), True
            save_json(SNAP_PATH, snap)
            return snap, True
        except Exception:
            if i == 0:
                time.sleep(2)
    _cache["live"] = False
    return None, False

def _refresh_bg():
    if _refreshing["on"]:
        return
    _refreshing["on"] = True
    def run():
        try:
            _attempt_snapshot()
        finally:
            _refreshing["on"] = False
    threading.Thread(target=run, daemon=True).start()

def get_snapshot(force=False):
    now = time.time()
    snap = _cache["snap"]
    if snap is not None and not force and now - _cache["ts"] < CACHE_TTL:
        return snap, _cache["live"]
    if snap is not None and not force:
        _refresh_bg()
        return snap, _cache["live"]
    got, live = _attempt_snapshot()
    if got is not None:
        return got, True
    if snap is not None:
        return snap, False
    empty = {"uploads": [], "current_id": None, "artmode": None}
    _cache["snap"], _cache["ts"], _cache["live"] = empty, time.time(), False
    return empty, False

async def _tv_op(op, arg):
    from samsungframetv.async_art import SamsungTVAsyncArt
    art = SamsungTVAsyncArt(host=HOST, port=TV_PORT, token_file=TOKEN,
                            name="Frame Artwork Manager", timeout=20)
    try:
        await asyncio.wait_for(art.open(), 30)
        if op == "show":
            if await art.get_artmode() != "on":
                return "not_artmode"
            await art.select_image(arg, show=True)
            return "shown"
        if op == "delete":
            await art.delete_list(arg)
            return "deleted"
        if op == "upload":
            return await art.upload(arg, matte="none")
        if op == "thumbs":
            r = await art.get_thumbnail(arg)
            ids = arg if isinstance(arg, list) else [arg]
            if isinstance(r, dict):
                by_id = r
            elif isinstance(r, (bytes, bytearray)):
                by_id = {ids[0]: bytes(r)} if ids else {}
            else:
                by_id = {t.get("content_id"):
                         t.get("image_binary") or t.get("data")
                         for t in (r or []) if isinstance(t, dict)}
            return [by_id.get(cid) for cid in ids]
        return None
    finally:
        try:
            await art.close()
        except Exception:
            pass

def tv_op(op, arg):
    with tv_lock:
        if op == "thumbs":
            import base64
            blobs = _run_helper(["--tv-op", "thumbs", json.dumps(arg)],
                                timeout=180)
            return [base64.b64decode(b) if b else None
                    for b in (blobs or [])]
        timeout = 300 if op == "upload" else 120
        return _run_helper(["--tv-op", op, json.dumps(arg)],
                           timeout=timeout)

def thumb_path(cid):
    return os.path.join(THUMB_DIR, cid + ".jpg")

def thumb_ok(cid):
    p = thumb_path(cid)
    return os.path.exists(p) and os.path.getsize(p) > 100

def write_thumb(cid, blob):
    if not blob or len(blob) < 100:
        return False
    tmp = thumb_path(cid) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, thumb_path(cid))
    return True

def make_local_thumb(cid, src):
    try:
        from PIL import Image
        img = Image.open(src)
        img.thumbnail((480, 480))
        tmp = thumb_path(cid) + ".tmp"
        img.convert("RGB").save(tmp, "JPEG", quality=80)
        os.replace(tmp, thumb_path(cid))
        return True
    except Exception:
        return False

def ensure_thumbs(ids):
    missing = [c for c in ids if not thumb_ok(c)]
    if not missing:
        return
    uploaded = load_json(UPLOADED_PATH, {})
    rest = []
    for c in missing:
        entry = uploaded.get(c)
        if entry and make_local_thumb(
                c, os.path.join(UPLOAD_DIR, entry["file"])):
            continue
        rest.append(c)
    if not rest:
        return
    try:
        blobs = tv_op("thumbs", rest)
        for cid, blob in zip(rest, blobs or []):
            write_thumb(cid, blob)
    except Exception:
        pass

def build_state():
    snap, live = get_snapshot()
    rot = load_json(ROT_PATH, {})
    pool = rot.get("pool")
    if not isinstance(pool, list):
        pool = list(snap["uploads"])
    poolset = set(pool)
    labels = load_labels()
    backups = backup_ids()
    def item(cid):
        return {"id": cid, "label": labels.get(cid, ""),
                "backup": cid in backups}
    liveset = set(snap["uploads"])
    return {
        "ok": True, "tv_reachable": live,
        "current_id": snap["current_id"], "artmode": snap["artmode"],
        "enabled": bool(rot.get("enabled", False)),
        "interval_min": rot.get("interval_min", 10),
        "mode": rot.get("mode", "shuffle"),
        "in": [item(c) for c in pool if c in liveset],
        "out": [item(c) for c in snap["uploads"]
                if c not in poolset],
    }

def mutate_pool(ids, add):
    rot = load_json(ROT_PATH, {})
    pool = rot.get("pool")
    if not isinstance(pool, list):
        snap, _ = get_snapshot()
        pool = list(snap["uploads"])
    if add:
        for cid in ids:
            if cid not in pool:
                pool.append(cid)
    else:
        drop = set(ids)
        pool = [c for c in pool if c not in drop]
    rot["pool"] = pool
    save_json(ROT_PATH, rot)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(PAGE_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(500, "page not installed yet", "text/plain")
            return
        if path == "/api/state":
            self._send(200, build_state())
            return
        if path.startswith("/thumb/"):
            cid = path.split("/")[2].split(".")[0]
            if "/" in cid or ".." in cid:
                self._send(400, {"ok": False})
                return
            if not thumb_ok(cid):
                try:
                    os.remove(thumb_path(cid))
                except Exception:
                    pass
                ensure_thumbs([cid])
            if thumb_ok(cid):
                with open(thumb_path(cid), "rb") as f:
                    self._send(200, f.read(), "image/jpeg")
            else:
                self._send(404, {"ok": False, "error": "no thumbnail"})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def _handle_upload(self):
        q = parse_qs(urlparse(self.path).query)
        name = (q.get("name") or ["photo"])[0]
        style = (q.get("style") or [""])[0]
        if style not in ("fill", "original", "classic", "modern"):
            # legacy framing parameter
            style = "fill" if (q.get("framing") or ["fit"])[0] == "crop" \
                else "original"
        slideshow = (q.get("slideshow") or ["1"])[0] \
            not in ("0", "false", "no")
        color = (250, 248, 242)
        raw_color = (q.get("color") or [""])[0]
        if len(raw_color) == 6 and all(
                c in "0123456789abcdefABCDEF" for c in raw_color):
            color = tuple(int(raw_color[i:i + 2], 16) for i in (0, 2, 4))
        crop = None
        try:
            vals = [float((q.get(k) or ["nan"])[0])
                    for k in ("fx", "fy", "fw", "fh")]
            if all(v == v for v in vals) and vals[2] > 0.02 and vals[3] > 0.02:
                crop = tuple(vals)
        except Exception:
            crop = None
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0 or n > 40 * 1024 * 1024:
            self._send(400, {"ok": False,
                             "error": "File missing or larger than 40 MB."})
            return
        data = self.rfile.read(n)
        stem = os.path.splitext(os.path.basename(name))[0]
        try:
            path = frame_and_save(data, style, stem, color, crop)
        except Exception as e:
            self._send(400, {"ok": False, "error":
                "Could not read that image (%s). JPEG and PNG work best."
                % type(e).__name__})
            return
        cid, err = None, None
        for attempt in range(2):
            try:
                cid = tv_op("upload", path)
                err = None
                break
            except Exception as e:
                err = e
                if attempt == 0:
                    time.sleep(3)
        if err is not None:
            try:
                os.remove(path)
            except Exception:
                pass
            self._send(500, {"ok": False, "error": "TV upload failed: %s" % err})
            return
        if not cid or not isinstance(cid, str):
            try:
                os.remove(path)
            except Exception:
                pass
            self._send(500, {"ok": False,
                             "error": "The TV did not accept the upload."})
            return
        label = " ".join(stem.replace("_", " ").replace("-", " ").split())[:120]
        uploaded = load_json(UPLOADED_PATH, {})
        uploaded[cid] = {"file": os.path.basename(path), "label": label}
        save_json(UPLOADED_PATH, uploaded)
        if label:
            labels = load_labels()
            labels[cid] = label
            save_json(LABELS_PATH, labels)
        make_local_thumb(cid, path)
        if slideshow:
            mutate_pool([cid], True)
        _attempt_snapshot()
        st = build_state()
        st["uploaded_id"] = cid
        self._send(200, st)

    def do_POST(self):
        if urlparse(self.path).path == "/api/upload":
            self._handle_upload()
            return
        data = self._body()
        path = urlparse(self.path).path
        try:
            if path == "/api/add":
                mutate_pool(data.get("ids", []), True)
                _cache["ts"] = 0
                self._send(200, build_state())
            elif path == "/api/remove":
                mutate_pool(data.get("ids", []), False)
                _cache["ts"] = 0
                self._send(200, build_state())
            elif path == "/api/delete":
                ids = data.get("ids", [])
                tv_op("delete", ids)
                mutate_pool(ids, False)
                labels = load_labels()
                uploaded = load_json(UPLOADED_PATH, {})
                for cid in ids:
                    labels.pop(cid, None)
                    entry = uploaded.pop(cid, None)
                    if entry:
                        try:
                            os.remove(os.path.join(UPLOAD_DIR, entry["file"]))
                        except Exception:
                            pass
                    try:
                        os.remove(thumb_path(cid))
                    except Exception:
                        pass
                save_json(LABELS_PATH, labels)
                save_json(UPLOADED_PATH, uploaded)
                _attempt_snapshot()
                self._send(200, build_state())
            elif path == "/api/label":
                labels = load_labels()
                labels[data["id"]] = str(data.get("label", ""))[:120]
                save_json(LABELS_PATH, labels)
                self._send(200, {"ok": True})
            elif path == "/api/show":
                r = tv_op("show", data.get("id"))
                if r == "not_artmode":
                    _cache["ts"] = 0
                    self._send(200, {"ok": False, "error":
                        "The TV is not in Art Mode right now."})
                else:
                    _attempt_snapshot()
                    self._send(200, build_state())
            elif path == "/api/next":
                p = subprocess.run([sys.executable, ROTATE_SCRIPT, "force"],
                                   capture_output=True, text=True, timeout=180)
                lines = (p.stdout or "").strip().splitlines()
                raw = lines[-1] if lines else ""
                _attempt_snapshot()
                st = build_state()
                m = re.search(r"rotated \S+ -> (\S+)", raw)
                if m:
                    name = load_labels().get(m.group(1)) or "Untitled"
                    st["message"] = "Now showing: %s" % name
                elif "not in art mode" in raw:
                    st["message"] = ("The TV isn't in Art Mode, "
                                     "so nothing changed.")
                elif "only one piece" in raw:
                    st["message"] = ("There's only one piece "
                                     "in the slideshow.")
                elif "empty pool" in raw:
                    st["message"] = ("The slideshow is empty — "
                                     "nothing to rotate to.")
                elif "tick failed" in raw:
                    st["message"] = ("Rotation failed — the TV may be "
                                     "off or unreachable.")
                else:
                    st["message"] = "Done."
                self._send(200, st)
            elif path == "/api/reorder":
                ids = data.get("ids", [])
                rot = load_json(ROT_PATH, {})
                pool = rot.get("pool")
                if not isinstance(pool, list) or len(ids) != len(pool) \
                        or set(ids) != set(pool):
                    self._send(400, {"ok": False, "error":
                        "That order does not match the current slideshow. "
                        "Reload the page and try again."})
                    return
                rot["pool"] = ids
                save_json(ROT_PATH, rot)
                self._send(200, build_state())
            elif path == "/api/settings":
                rot = load_json(ROT_PATH, {})
                if "mode" in data:
                    if data["mode"] not in ("shuffle", "ordered"):
                        self._send(400, {"ok": False, "error":
                            "Mode must be shuffle or ordered."})
                        return
                    rot["mode"] = data["mode"]
                if "interval_min" in data:
                    v = data["interval_min"]
                    if not isinstance(v, int) or isinstance(v, bool) \
                            or v < 1 or v > 60:
                        self._send(400, {"ok": False, "error":
                            "Interval must be a whole number of minutes, 1 to 60."})
                        return
                    rot["interval_min"] = v
                if "enabled" in data:
                    rot["enabled"] = bool(data["enabled"])
                save_json(ROT_PATH, rot)
                self._send(200, {"ok": True,
                                 "enabled": rot.get("enabled", False),
                                 "interval_min": rot.get("interval_min", 10),
                                 "mode": rot.get("mode", "shuffle")})
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})

def prime_thumbs():
    try:
        snap, live = get_snapshot()
        if live:
            ensure_thumbs(snap["uploads"])
    except Exception:
        pass

def _helper_main(argv):
    """Entry point for the per-operation helper processes (see
    _run_helper). Performs exactly one TV operation and prints one
    JSON line: {"ok": true, "result": ...} or {"ok": false, ...}."""
    import base64
    try:
        if argv[0] == "--snapshot":
            print(json.dumps({"ok": True,
                              "result": asyncio.run(_snapshot())}))
            return 0
        if argv[0] == "--tv-op":
            op = argv[1]
            arg = json.loads(argv[2]) if len(argv) > 2 else None
            r = asyncio.run(_tv_op(op, arg))
            if op == "thumbs":
                r = [base64.b64encode(b).decode() if b else None
                     for b in (r or [])]
            print(json.dumps({"ok": True, "result": r}))
            return 0
    except Exception as e:
        print(json.dumps({"ok": False,
                          "error": str(e) or repr(e)}))
        return 0
    return 2

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--snapshot", "--tv-op"):
        sys.exit(_helper_main(sys.argv[1:]))
    if not HOST:
        raise SystemExit(
            "No TV configured. Create %s with your TV's address "
            "(see config.example.json)."
            % os.path.join(DATA_DIR, "config.json"))
    for d in (DATA_DIR, UPLOAD_DIR, THUMB_DIR):
        os.makedirs(d, exist_ok=True)
    load_labels()
    threading.Thread(target=prime_thumbs, daemon=True).start()
    ThreadingHTTPServer((CFG["bind"], PORT), Handler).serve_forever()
