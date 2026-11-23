import asyncio, hashlib, json, os, re, subprocess, sys, threading, time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

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
JOURNAL_PATH = os.path.join(DATA_DIR, "uploads_journal.json")
SAVED_PATH = os.path.join(DATA_DIR, "discover_saved.json")
DISCOVER_DIR = os.path.join(DATA_DIR, "discover_cache")
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
        if op == "settings_get":
            out = {}
            for name in ("get_brightness", "get_color_temperature",
                         "get_artmode_settings", "get_slideshow_status",
                         "get_auto_rotation_status"):
                try:
                    out[name] = await getattr(art, name)()
                except Exception as e:
                    out[name] = {"error": str(e) or repr(e)}
            return out
        if op == "settings_set":
            errors = {}
            spec = [("brightness", "set_brightness", int),
                    ("color_temperature", "set_color_temperature", int),
                    ("motion_sensitivity", "set_motion_sensitivity", str),
                    ("motion_timer", "set_motion_timer", str),
                    ("brightness_sensor_setting",
                     "set_brightness_sensor_setting", str),
                    ("slideshow_status", "set_slideshow_status", str),
                    ("auto_rotation_status",
                     "set_auto_rotation_status", str)]
            for key, method, conv in spec:
                if key not in (arg or {}):
                    continue
                try:
                    await getattr(art, method)(conv(arg[key]))
                except Exception as e:
                    errors[key] = str(e) or repr(e)
            return {"errors": errors}
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

def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def load_journal():
    j = load_json(JOURNAL_PATH, [])
    return j if isinstance(j, list) else []

def find_duplicate(sha):
    """Content id of an identical framed image that is still on the TV,
    or None. Dedupe is by the framed bytes, so the same photo with a
    different mat counts as a different piece (as it should)."""
    uploaded = load_json(UPLOADED_PATH, {})
    snap, _ = get_snapshot()
    on_tv = set(snap.get("uploads", []))
    for cid, entry in uploaded.items():
        if entry.get("sha256") == sha and cid in on_tv:
            return cid
    return None

def deliver_image(path, label, slideshow):
    """Upload one framed image file to the TV, with dedupe and with
    verification against the TV's own artwork list before the upload is
    recorded. Returns ("ok" | "duplicate" | "unconfirmed", content_id).
    Raises on hard failure; the caller still owns the local file then.
    An unconfirmed upload is written to the journal and blocks further
    uploads until the user resolves it (see /api/journal/resolve)."""
    sha = _sha256_file(path)
    dup = find_duplicate(sha)
    if dup:
        return ("duplicate", dup)
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
        raise err
    if not cid or not isinstance(cid, str):
        raise RuntimeError("The TV did not accept the upload.")
    snap, _live = _attempt_snapshot()
    on_tv = set((snap or {}).get("uploads", []))
    if cid not in on_tv:
        journal = load_journal()
        journal.append({"ts": int(time.time()),
                        "file": os.path.basename(path), "sha256": sha,
                        "label": label, "cid": cid,
                        "status": "unconfirmed"})
        save_json(JOURNAL_PATH, journal)
        return ("unconfirmed", cid)
    uploaded = load_json(UPLOADED_PATH, {})
    uploaded[cid] = {"file": os.path.basename(path), "label": label,
                     "sha256": sha}
    save_json(UPLOADED_PATH, uploaded)
    if label:
        labels = load_labels()
        labels[cid] = label
        save_json(LABELS_PATH, labels)
    make_local_thumb(cid, path)
    if slideshow:
        mutate_pool([cid], True)
    return ("ok", cid)

def backfill_hashes():
    """Give older uploads a content hash so dedupe covers them too."""
    try:
        uploaded = load_json(UPLOADED_PATH, {})
        changed = False
        for entry in uploaded.values():
            if not entry.get("sha256"):
                p = os.path.join(UPLOAD_DIR, entry.get("file", ""))
                if os.path.exists(p):
                    entry["sha256"] = _sha256_file(p)
                    changed = True
        if changed:
            save_json(UPLOADED_PATH, uploaded)
    except Exception:
        pass

# --- Discover: public museum collections --------------------------------
# Three open-access APIs, no keys: The Met, the Cleveland Museum of Art
# and the Art Institute of Chicago. Cards are normalized to one shape;
# the full image is downloaded by this server (never the browser) when
# a piece is previewed or sent.

UA = {"User-Agent": "FrameArtworkManager/1.1 (personal artwork browser)"}
MET_API = "https://collectionapi.metmuseum.org/public/collection/v1"
MET_SEARCH_API = ("https://collectionapi.metmuseum.org"
                  "/public/collection/v1.1")
CMA_API = "https://openaccess-api.clevelandart.org/api/artworks"
AIC_API = "https://api.artic.edu/api/v1/artworks"
_met_cache = {}
_disc_cache = {}

def _http_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _http_bytes(url, timeout=90):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _dim(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def _badge(w, h):
    w, h = _dim(w), _dim(h)
    if w and h and w >= 3840 and h >= 2160:
        return "Exact 4K" if (w, h) == (3840, 2160) else "4K crop possible"
    return ""

def _pool_map(fn, items, workers=6):
    from concurrent.futures import ThreadPoolExecutor
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))

def _met_object(oid):
    if oid in _met_cache:
        return _met_cache[oid]
    o = _http_json("%s/objects/%s" % (MET_API, oid))
    _met_cache[oid] = o
    return o

def _met_card(o):
    if not o or not o.get("isPublicDomain") or not o.get("primaryImage"):
        return None
    return {"source": "met", "id": str(o["objectID"]),
            "title": o.get("title") or "Untitled",
            "artist": o.get("artistDisplayName") or "",
            "date": o.get("objectDate") or "",
            "medium": o.get("medium") or "",
            "thumb_url": o.get("primaryImageSmall") or o["primaryImage"],
            "image_url": o["primaryImage"], "send_url": o["primaryImage"],
            "width": None, "height": None, "badge": ""}

def discover_met(q, limit=24):
    # The Met retired the v1 search endpoint (410 Gone); search lives
    # under v1.1 while object records remain on v1.
    url = "%s/search?hasImages=true&isPublicDomain=true&q=%s" % (
        MET_SEARCH_API, quote(q or "landscape"))
    ids = (_http_json(url).get("objectIDs") or [])[:limit]
    def one(oid):
        try:
            return _met_card(_met_object(oid))
        except Exception:
            return None
    return [c for c in _pool_map(one, ids) if c]

def discover_cma(q, limit=24):
    url = "%s/?has_image=1&cc0=1&limit=%d&q=%s" % (
        CMA_API, limit, quote(q or ""))
    cards = []
    for it in _http_json(url).get("data", []):
        imgs = it.get("images") or {}
        pr, web = imgs.get("print") or {}, imgs.get("web") or {}
        if not pr.get("url"):
            continue
        creators = it.get("creators") or []
        w, h = _dim(pr.get("width")), _dim(pr.get("height"))
        cards.append({
            "source": "cma", "id": str(it["id"]),
            "title": it.get("title") or "Untitled",
            "artist": creators[0].get("description", "") if creators else "",
            "date": it.get("creation_date") or "",
            "medium": it.get("technique") or "",
            "thumb_url": web.get("url") or pr["url"],
            "image_url": web.get("url") or pr["url"],
            "send_url": pr["url"],
            "width": w, "height": h, "badge": _badge(w, h)})
    return cards

def _aic_card(it, iiif):
    iid = it.get("image_id")
    if not iid or not it.get("is_public_domain"):
        return None
    return {"source": "aic", "id": str(it["id"]),
            "title": it.get("title") or "Untitled",
            "artist": it.get("artist_title") or "",
            "date": it.get("date_display") or "",
            "medium": it.get("medium_display") or "",
            "thumb_url": "%s/%s/full/400,/0/default.jpg" % (iiif, iid),
            "image_url": "%s/%s/full/1200,/0/default.jpg" % (iiif, iid),
            "send_url": "%s/%s/full/3840,/0/default.jpg" % (iiif, iid),
            "width": None, "height": None, "badge": ""}

def discover_aic(q, limit=24):
    fields = ("id,title,artist_title,date_display,medium_display,"
              "image_id,is_public_domain")
    url = "%s/search?limit=%d&fields=%s&q=%s" % (
        AIC_API, limit, fields, quote(q or ""))
    r = _http_json(url)
    iiif = (r.get("config") or {}).get("iiif_url",
                                        "https://www.artic.edu/iiif/2")
    return [c for c in (_aic_card(it, iiif) for it in r.get("data", []))
            if c]

def discover_featured():
    key = "featured"
    if key in _disc_cache and time.time() - _disc_cache[key][0] < 3600:
        return _disc_cache[key][1]
    cards = []
    for fn, q in ((discover_met, "Monet"), (discover_cma, "landscape"),
                  (discover_aic, "Monet")):
        try:
            cards += fn(q, 8)
        except Exception:
            pass
    _disc_cache[key] = (time.time(), cards)
    return cards

def discover_search(source, q):
    if source == "met":
        return discover_met(q)
    if source == "cma":
        return discover_cma(q)
    if source == "aic":
        return discover_aic(q)
    return discover_featured()

def discover_resolve(source, oid):
    """Full card (with send_url) for one museum object."""
    if source == "met":
        return _met_card(_met_object(int(oid)))
    if source == "cma":
        it = _http_json("%s/%s" % (CMA_API, oid)).get("data") or {}
        imgs = it.get("images") or {}
        pr = imgs.get("print") or {}
        if not pr.get("url"):
            return None
        creators = it.get("creators") or []
        return {"source": "cma", "id": str(it.get("id", oid)),
                "title": it.get("title") or "Untitled",
                "artist": creators[0].get("description", "")
                if creators else "",
                "date": it.get("creation_date") or "",
                "medium": it.get("technique") or "",
                "thumb_url": (imgs.get("web") or {}).get("url") or pr["url"],
                "image_url": (imgs.get("web") or {}).get("url") or pr["url"],
                "send_url": pr["url"], "width": _dim(pr.get("width")),
                "height": _dim(pr.get("height")),
                "badge": _badge(pr.get("width"), pr.get("height"))}
    if source == "aic":
        fields = ("id,title,artist_title,date_display,medium_display,"
                  "image_id,is_public_domain")
        r = _http_json("%s/%s?fields=%s" % (AIC_API, oid, fields))
        iiif = (r.get("config") or {}).get(
            "iiif_url", "https://www.artic.edu/iiif/2")
        return _aic_card(r.get("data") or {}, iiif)
    return None

def discover_image_bytes(source, oid):
    """The full-size image for one museum piece, disk-cached."""
    os.makedirs(DISCOVER_DIR, exist_ok=True)
    path = os.path.join(DISCOVER_DIR, "%s_%s.jpg" % (source, oid))
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        with open(path, "rb") as f:
            return f.read()
    card = discover_resolve(source, oid)
    if not card:
        raise RuntimeError("That work could not be found.")
    data = _http_bytes(card["send_url"])
    with open(path, "wb") as f:
        f.write(data)
    return data

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
        "journal": load_journal(),
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
        if path == "/api/discover":
            q = parse_qs(urlparse(self.path).query)
            source = (q.get("source") or ["featured"])[0]
            if source not in ("featured", "met", "cma", "aic"):
                source = "featured"
            term = (q.get("q") or [""])[0]
            try:
                self._send(200, {"ok": True,
                                 "items": discover_search(source, term)})
            except Exception as e:
                self._send(502, {"ok": False, "error":
                    "The museum service did not answer (%s)." % e})
            return
        if path == "/api/discover/saved":
            items = load_json(SAVED_PATH, [])
            self._send(200, {"ok": True,
                             "items": items if isinstance(items, list)
                             else []})
            return
        if path == "/api/discover/image":
            q = parse_qs(urlparse(self.path).query)
            source = (q.get("source") or [""])[0]
            oid = (q.get("id") or [""])[0]
            if source not in ("met", "cma", "aic") or not oid.isdigit():
                self._send(400, {"ok": False})
                return
            try:
                self._send(200, discover_image_bytes(source, oid),
                           "image/jpeg")
            except Exception as e:
                self._send(502, {"ok": False, "error": str(e)})
            return
        if path == "/api/tvsettings":
            try:
                self._send(200, {"ok": True,
                                 "settings": tv_op("settings_get", None)})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
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
        if load_journal():
            self._send(409, {"ok": False, "error":
                "An earlier upload could not be confirmed on the TV. "
                "Resolve it in the notice at the top of the page before "
                "uploading more."})
            return
        stem = os.path.splitext(os.path.basename(name))[0]
        try:
            path = frame_and_save(data, style, stem, color, crop)
        except Exception as e:
            self._send(400, {"ok": False, "error":
                "Could not read that image (%s). JPEG and PNG work best."
                % type(e).__name__})
            return
        label = " ".join(stem.replace("_", " ").replace("-", " ").split())[:120]
        try:
            status, cid = deliver_image(path, label, slideshow)
        except Exception as e:
            try:
                os.remove(path)
            except Exception:
                pass
            self._send(500, {"ok": False,
                             "error": "TV upload failed: %s" % e})
            return
        if status == "duplicate":
            try:
                os.remove(path)
            except Exception:
                pass
        _attempt_snapshot()
        st = build_state()
        st["uploaded_id"] = cid
        if status == "duplicate":
            st["message"] = "That exact image is already on the Frame."
        elif status == "unconfirmed":
            st["message"] = ("The TV took the upload, but the piece could "
                             "not be confirmed in its list — see the "
                             "notice at the top of the page.")
        self._send(200, st)

    def _handle_discover_send(self, data):
        source = data.get("source")
        oid = str(data.get("id") or "")
        if source not in ("met", "cma", "aic") or not oid.isdigit():
            self._send(400, {"ok": False, "error": "Unknown artwork."})
            return
        if load_journal():
            self._send(409, {"ok": False, "error":
                "An earlier upload could not be confirmed on the TV. "
                "Resolve it in the notice at the top of the page before "
                "uploading more."})
            return
        style = data.get("style")
        if style not in ("fill", "original", "classic", "modern"):
            style = "fill"
        slideshow = bool(data.get("slideshow", True))
        color = (250, 248, 242)
        raw_color = str(data.get("color") or "")
        if len(raw_color) == 6 and all(
                c in "0123456789abcdefABCDEF" for c in raw_color):
            color = tuple(int(raw_color[i:i + 2], 16) for i in (0, 2, 4))
        crop = None
        try:
            vals = [float(data[k]) for k in ("fx", "fy", "fw", "fh")]
            if vals[2] > 0.02 and vals[3] > 0.02:
                crop = tuple(vals)
        except Exception:
            crop = None
        try:
            card = discover_resolve(source, oid)
            if not card:
                raise RuntimeError("not found")
            data_bytes = discover_image_bytes(source, oid)
        except Exception as e:
            self._send(502, {"ok": False, "error":
                "Could not download that artwork (%s)." % e})
            return
        title = card["title"]
        artist = card.get("artist") or ""
        label = (title + (" — " + artist if artist else ""))[:120]
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title)[:60] or "artwork"
        try:
            path = frame_and_save(data_bytes, style, stem, color, crop)
        except Exception as e:
            self._send(400, {"ok": False, "error":
                "Could not read that image (%s)." % type(e).__name__})
            return
        try:
            status, cid = deliver_image(path, label, slideshow)
        except Exception as e:
            try:
                os.remove(path)
            except Exception:
                pass
            self._send(500, {"ok": False,
                             "error": "TV upload failed: %s" % e})
            return
        if status == "duplicate":
            try:
                os.remove(path)
            except Exception:
                pass
        _attempt_snapshot()
        st = build_state()
        st["uploaded_id"] = cid
        if status == "duplicate":
            st["message"] = "That exact image is already on the Frame."
        elif status == "unconfirmed":
            st["message"] = ("The TV took the upload, but the piece could "
                             "not be confirmed in its list — see the "
                             "notice at the top of the page.")
        else:
            st["message"] = "Sent to the Frame: %s" % label
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
            elif path == "/api/discover/save":
                item = data.get("item") or {}
                key = (item.get("source"), str(item.get("id")))
                saved = load_json(SAVED_PATH, [])
                if not isinstance(saved, list):
                    saved = []
                saved = [s for s in saved
                         if (s.get("source"), str(s.get("id"))) != key]
                if data.get("saved"):
                    saved.append(item)
                save_json(SAVED_PATH, saved)
                self._send(200, {"ok": True, "items": saved})
            elif path == "/api/discover/send":
                self._handle_discover_send(data)
            elif path == "/api/journal/resolve":
                sha = data.get("sha256")
                action = data.get("action")
                journal = load_journal()
                entry = next((j for j in journal
                              if j.get("sha256") == sha), None)
                if entry is None:
                    self._send(404, {"ok": False,
                                     "error": "That entry is already gone."})
                    return
                if action not in ("found", "missing"):
                    self._send(400, {"ok": False,
                                     "error": "Unknown action."})
                    return
                journal = [j for j in journal
                           if j.get("sha256") != sha]
                save_json(JOURNAL_PATH, journal)
                if action == "found":
                    cid = entry.get("cid")
                    uploaded = load_json(UPLOADED_PATH, {})
                    uploaded[cid] = {"file": entry.get("file"),
                                     "label": entry.get("label"),
                                     "sha256": sha}
                    save_json(UPLOADED_PATH, uploaded)
                    if entry.get("label"):
                        labels = load_labels()
                        labels[cid] = entry["label"]
                        save_json(LABELS_PATH, labels)
                    make_local_thumb(cid, os.path.join(
                        UPLOAD_DIR, entry.get("file", "")))
                else:
                    try:
                        os.remove(os.path.join(UPLOAD_DIR,
                                               entry.get("file", "")))
                    except Exception:
                        pass
                _attempt_snapshot()
                self._send(200, build_state())
            elif path == "/api/tvsettings":
                r = tv_op("settings_set", data)
                fresh = tv_op("settings_get", None)
                self._send(200, {"ok": True, "settings": fresh,
                                 "errors": (r or {}).get("errors", {})})
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
    for d in (DATA_DIR, UPLOAD_DIR, THUMB_DIR, DISCOVER_DIR):
        os.makedirs(d, exist_ok=True)
    load_labels()
    threading.Thread(target=prime_thumbs, daemon=True).start()
    threading.Thread(target=backfill_hashes, daemon=True).start()
    ThreadingHTTPServer((CFG["bind"], PORT), Handler).serve_forever()
