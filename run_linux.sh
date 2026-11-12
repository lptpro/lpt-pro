#!/usr/bin/env bash
# LPT Pro - run from source on Linux.
# Sets up a private Python environment in .venv the first time, then starts the app.
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"

# The iPhone USB service on Linux is the free usbmuxd (no Apple software needed).
if ! command -v usbmuxd >/dev/null 2>&1 && [ ! -x /usr/sbin/usbmuxd ]; then
  echo "The iPhone USB service (usbmuxd) isn't installed. Install it with:"
  echo "    sudo apt install usbmuxd"
  echo "then unplug the iPhone and plug it back in."
  [ -t 0 ] && read -r -p "Press Enter to continue anyway… " _
fi

if [ ! -x .venv/bin/python ]; then
  if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c "import ensurepip" 2>/dev/null; then
    echo "Python's environment tool is missing. Install it with:"
    echo "    sudo apt install python3-venv"
    exit 1
  fi
  echo "First run: setting things up. This takes a few minutes, only once..."
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

# App-menu entry (and a desktop icon if there is a Desktop folder)
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
rm -f "$APPS/iphone-photo-transfer.desktop"          # name used by test builds
DESK="$APPS/io.github.lptpro.LPTPro.desktop"
cat > "$DESK" <<EOD
[Desktop Entry]
Type=Application
Name=LPT Pro
GenericName=Local Photo Transfer
Comment=Copy photos and videos from your iPhone
Exec=sh -c 'cd "$HERE" && .venv/bin/python -m lptpro'
Icon=$HERE/lptpro/icon.png
Terminal=false
Categories=Graphics;Photography;
EOD
chmod +x "$DESK"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
  rm -f "$DESKTOP_DIR/iphone-photo-transfer.desktop" "$DESKTOP_DIR/lpt-pro.desktop"
  cp "$DESK" "$DESKTOP_DIR/" && chmod +x "$DESKTOP_DIR/io.github.lptpro.LPTPro.desktop"
  gio set "$DESKTOP_DIR/io.github.lptpro.LPTPro.desktop" metadata::trusted true 2>/dev/null || true
fi

exec .venv/bin/python -m lptpro "$@"
