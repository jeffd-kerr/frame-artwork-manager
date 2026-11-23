# Frame Artwork Manager

A small web app for managing the artwork on a Samsung The Frame TV —
the manager Samsung should have included.

The Frame's own apps make it painful to curate a personal collection:
no easy way to see everything you have uploaded, decide what is in the
slideshow, label pieces, or control how the slideshow rotates. This app
runs on a computer on your home network (a Raspberry Pi, a Mac, any
always-on machine) and gives you one page for all of it.

## Features

- **Two zones** — *In the slideshow* and *Not in the slideshow* — with
  every piece on your TV shown as a thumbnail card. Click to select,
  drag and drop between zones, or use the buttons.
- **Rotation you control** — the TV's built-in slideshow timer cannot
  be switched on through its API on some firmware (on the LS03B it
  refuses with an error), so rotation is driven from your computer
  instead: a tiny script runs once a minute and advances the art while
  the TV is in Art Mode. Choose **Shuffle** or **Ordered** (in Ordered
  mode, the grid order *is* the play order), and set the interval from
  1 to 60 minutes.
- **Show on TV** — put any piece on screen immediately, plus a
  **Next piece** button that advances the slideshow on demand.
- **Uploads with real mats** — upload a photo and frame it at 4K in
  four styles: *Fill Screen* (full-bleed crop), *Original Photo*
  (native aspect floating on a mat with a drop shadow), *Classic Mat*
  (photo window on a mat with a thin white keyline), and *Modern Mat*
  (photo window with a soft shadow, no keyline). Thirteen mat colors,
  drag-to-pan and zoom in a live preview. Pieces uploaded here keep a
  local copy, so they can be restored if the TV ever loses them. New
  uploads join the slideshow by default — uncheck *Add to the
  slideshow* in the upload editor to keep one out.
- **Labels** — click a piece's name to rename it. Unlabeled pieces show
  as *Untitled*.
- **Discover museum artwork** — a Discover tab browses and searches
  open-access works from The Met, the Cleveland Museum of Art and the
  Art Institute of Chicago (no accounts or keys), with a mixed Featured
  view and a Saved shortlist. Preview a piece, then send it to the TV
  through the same framing editor as uploads.
- **TV settings** — a settings tab reads and changes the TV's own Art
  Mode settings where the firmware allows: brightness, color
  temperature, the brightness (ambient light) sensor, motion sensor
  sensitivity and timer, and the TV's built-in slideshow. Changes the
  TV refuses are reported, never hidden.
- **Upload integrity** — every framed upload is hashed; sending the
  identical image again (same framing) is skipped while the piece is
  still on the TV. Each upload is verified against the TV's artwork
  list before it is recorded, and one that cannot be confirmed appears
  in a notice at the top of the page and pauses further uploads until
  you resolve it.
- **Delete with a safety net** — deleting asks for confirmation and is
  permanent on the TV. Each card shows whether a local backup copy
  exists.
- **Works when the TV is off** — the page loads from the last known
  state and tells you plainly that the TV is unreachable, instead of
  failing.

## Requirements

- A Samsung The Frame TV, 2021 or newer, on your network, with Art
  Mode. (Developed against a 2022 LS03B; the underlying
  [`samsungframetv`](https://pypi.org/project/samsungframetv/) library
  supports the 2021–2024 art API models, including the 2024 LS03D.)
- Python 3.10 or newer on a computer on the same network as the TV.
  The computer has to be awake for the page to load and for rotation
  to advance.
- The TV's IP address (a DHCP reservation or a static address is a
  good idea).

## Install

```sh
git clone https://github.com/jeffd-kerr/frame-artwork-manager.git
cd frame-artwork-manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

mkdir -p ~/.frame-artwork
cp config.example.json ~/.frame-artwork/config.json
# edit ~/.frame-artwork/config.json and set "tv_host" to your TV's IP
```

Start the web app:

```sh
.venv/bin/python frame_app.py
```

Open `http://<that-computer>:8099` in a browser. The **first time**
the app talks to your TV, the TV shows an **Allow** prompt — press
Allow on the TV once; later connections are silent. (Depending on the
model and firmware, a pairing token may be stored as `tv_token.txt`
in the data directory; on some models no token file appears and it
simply works.)

Then set the rotation script to run every minute — pick one:

- **cron:** `* * * * * /path/to/frame-artwork-manager/.venv/bin/python /path/to/frame-artwork-manager/frame_rotate.py >> ~/.frame-artwork/rotation.log 2>&1`
- **systemd:** see `examples/frame-artwork-rotation.service` and
  `examples/frame-artwork-rotation.timer`.
- **macOS launchd:** see `examples/com.frameartwork.rotation.plist`.

To keep the web app itself running all the time, see
`examples/frame-artwork.service` (systemd) or
`examples/com.frameartwork.app.plist` (launchd). **On a Mac, use the
app bundle in [Running on a Mac](#running-on-a-mac) instead** — a
plain background process runs into a macOS permission wall, described
there.

## Running on a Mac

macOS has a privacy feature — **Local Network** permission — that
decides which apps may talk to devices on your home network. It has a
sharp edge for a server like this one:

- Start the server from a terminal or an SSH session and leave it
  running, and it works — until the session that launched it ends.
  From then on, every attempt to reach the TV fails with
  `[Errno 65] No route to host`. The web page still loads and the app
  itself is fine; macOS is silently refusing its connections to the
  TV. (A `launchd` agent started the same detached way can hit the
  same wall.)
- There is nothing to switch on to fix the bare Python process: it
  never appears in **Settings → Privacy & Security → Local Network**,
  because only apps can hold that permission.

The reliable route is to run the server as a small app, which is what
`macos/build_app.sh` builds for you — a `FrameArtwork.app` bundle
(a tiny compiled launcher that starts the Python server) with a
local-network usage description:

```sh
macos/build_app.sh          # builds ~/Applications/FrameArtwork.app
open ~/Applications/FrameArtwork.app
```

The first time it talks to your TV, macOS asks to allow **"python"**
to find and connect to devices on your local network — click
**Allow**. The permission attaches to the Python binary itself; you
can review or revoke it later in **Settings → Privacy & Security →
Local Network**.

To start the server whenever you log in, add **Frame Artwork
Manager** under **Settings → General → Login Items**. (It runs as a
background app with no Dock icon; quit it from Activity Monitor, or
by removing it and logging out.)

This was verified on macOS 27: the same server that failed within
minutes when left running from an SSH session runs indefinitely once
launched from the app bundle.

## Configuration

All state lives in one data directory: `$FRAME_DATA_DIR` if set, else
`~/.frame-artwork`. It holds `config.json`, your labels, thumbnails,
uploaded originals, and the rotation state — back up that directory
and you have backed up everything except the code.

`config.json`:

| Key | Meaning | Default |
| --- | --- | --- |
| `tv_host` | The TV's IP address or hostname | *(required)* |
| `tv_port` | The TV's art API port | `8002` |
| `port` | Port the web page is served on | `8099` |
| `bind` | Interface the web server binds to | `0.0.0.0` |

`FRAME_TV_HOST` in the environment overrides `tv_host`, which is
handy for testing.

There is no login on the web page — run it on a trusted home network
only.

## Good to know

- The TV must be **on and in Art Mode** for changes (show, rotate,
  upload, delete) to take effect. While you are watching TV, controls
  politely wait.
- The Frame's ambient light sensor can power the TV **completely
  off** in a dark room — it drops off the network entirely and comes
  back when the room lights up. If the app suddenly says the TV is
  unreachable at night, that is probably why, not a fault.
- The TV's **motion detector** can power it off the same way: it
  turns the TV off when it senses no motion for a set period, and a
  short period (5 minutes, say) drops it off the network in any
  empty room. Both this and the light setting live in the TV's Art
  Mode settings; if the app keeps losing the TV, check them. A long
  motion period (4 hours, for example) keeps the TV reachable
  without leaving it on around the clock.
- Full-resolution images **cannot be downloaded back off the TV** —
  only thumbnails. That is why the app keeps a framed local copy of
  everything uploaded through it (cards show whether a backup
  exists). Pieces you added with other apps exist only on the TV;
  deleting one of those here is final.
- Uploads are framed to 3840×2160 (4K). On a smaller Frame model the
  TV scales them down.
- Mat geometry: the matted styles use an even ~1 inch margin on all
  four sides at 4K, matching the mats Samsung's own upload flow
  produces.

## Layout

| File | What it is |
| --- | --- |
| `frame_app.py` | The web server and TV API layer (stdlib HTTP, no framework) |
| `page.html` | The single-page interface |
| `frame_rotate.py` | The once-a-minute rotation tick |
| `config.example.json` | Starter configuration |
| `examples/` | systemd and launchd units for running both pieces |
| `macos/` | `build_app.sh` + launcher source for the macOS app bundle (see *Running on a Mac*) |

## License

MIT — see [LICENSE](LICENSE).
