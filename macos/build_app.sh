#!/bin/bash
# Build "Frame Artwork Manager.app" — a small app bundle that runs the
# Frame Artwork Manager server on macOS.
#
# Why: macOS's Local Network privacy only grants durable network
# access to apps. A server started from a terminal or SSH session and
# left running loses access to your TV once the session ends (every
# TV operation then fails with "No route to host"). Wrapped in an app
# bundle and launched like a normal app, the server keeps its access.
# See "Running on a Mac" in the README.
#
# Usage:
#   macos/build_app.sh
#
# Environment overrides:
#   APP_OUT    where to put the .app   (default: ~/Applications)
#   DATA_DIR   the app's data dir      (default: $FRAME_DATA_DIR or ~/.frame-artwork)
#   BUNDLE_ID  bundle identifier       (default: local.frameartwork.manager)
#
# Requires the Xcode Command Line Tools (for cc and codesign):
#   xcode-select --install
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_OUT="${APP_OUT:-$HOME/Applications}"
DATA_DIR="${DATA_DIR:-${FRAME_DATA_DIR:-$HOME/.frame-artwork}}"
BUNDLE_ID="${BUNDLE_ID:-local.frameartwork.manager}"
APP="$APP_OUT/FrameArtwork.app"
PYTHON="$REPO_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "error: $PYTHON not found — run the Install steps in the README first." >&2
    exit 1
fi
if ! command -v cc >/dev/null; then
    echo "error: cc not found — install the Xcode Command Line Tools:" >&2
    echo "  xcode-select --install" >&2
    exit 1
fi

mkdir -p "$DATA_DIR" "$APP_OUT"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

# Bake this machine's paths into the launcher and compile it.
sed -e "s|@APP_DIR@|$REPO_DIR|g" \
    -e "s|@DATA_DIR@|$DATA_DIR|g" \
    -e "s|@PYTHON@|$PYTHON|g" \
    "$REPO_DIR/macos/launcher.c" > "$BUILD_DIR/launcher.c"
cc -O2 -o "$BUILD_DIR/FrameArtwork" "$BUILD_DIR/launcher.c"

# Assemble the bundle.
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BUILD_DIR/FrameArtwork" "$APP/Contents/MacOS/FrameArtwork"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>FrameArtwork</string>
  <key>CFBundleDisplayName</key><string>Frame Artwork Manager</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>FrameArtwork</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>Frame Artwork Manager needs to reach your Samsung Frame TV on your local network to show and upload artwork.</string>
</dict>
</plist>
PLIST

# Ad-hoc signature: enough for macOS to treat the bundle as one app.
codesign --force --deep --sign - "$APP"

echo "Built $APP"
echo
echo "Next steps:"
echo "  1. Launch it:            open \"$APP\""
echo "  2. The first time it talks to your TV, macOS asks to allow"
echo "     \"python\" to find and connect to devices on your local"
echo "     network — click Allow. (The permission attaches to the"
echo "     Python binary; you can review it later under Settings →"
echo "     Privacy & Security → Local Network.)"
echo "  3. Open http://localhost:8099 and check the TV shows as reachable."
echo "  4. To start it at login: Settings → General → Login Items → +"
echo "     and add Frame Artwork Manager."
