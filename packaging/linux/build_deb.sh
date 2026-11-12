#!/usr/bin/env bash
# Build a .deb from the PyInstaller output.
#   Usage: packaging/linux/build_deb.sh <dist/LPTPro folder> <output dir>
# Installs to /opt/lpt-pro, with an app-menu entry and the `lpt-pro` command.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DIST="$(cd "$1" && pwd)"
OUT="$(mkdir -p "$2" && cd "$2" && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/lptpro/__init__.py")"
ARCH="$(dpkg --print-architecture)"
APPID=io.github.lptpro.LPTPro
PKG="$(mktemp -d)/lpt-pro_${VERSION}_${ARCH}"

mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin" "$PKG/usr/share/applications" \
         "$PKG/usr/share/metainfo" "$PKG/usr/share/doc/lpt-pro"
cp -a "$DIST" "$PKG/opt/lpt-pro"
ln -s /opt/lpt-pro/LPTPro "$PKG/usr/bin/lpt-pro"
install -m644 "$HERE/$APPID.desktop" "$PKG/usr/share/applications/"
install -m644 "$HERE/$APPID.metainfo.xml" "$PKG/usr/share/metainfo/"
for n in 48 64 128 256 512; do
  d="$PKG/usr/share/icons/hicolor/${n}x${n}/apps"; mkdir -p "$d"
  install -m644 "$HERE/icons/$n.png" "$d/$APPID.png"
done
install -m644 "$ROOT/LICENSE" "$PKG/usr/share/doc/lpt-pro/copyright"
install -m644 "$ROOT/THIRD-PARTY-NOTICES.md" "$PKG/usr/share/doc/lpt-pro/"

SIZE="$(du -sk "$PKG" | cut -f1)"
cat > "$PKG/DEBIAN/control" <<CTRL
Package: lpt-pro
Version: $VERSION
Architecture: $ARCH
Maintainer: LPT Pro contributors <noreply@github.com>
Installed-Size: $SIZE
Depends: usbmuxd, libc6 (>= 2.35), libgl1, libegl1, libfontconfig1, libxkbcommon0, libxkbcommon-x11-0, libxcb-cursor0, libdbus-1-3
Section: graphics
Priority: optional
Homepage: https://github.com/lptpro/lpt-pro
Description: Copy photos and videos from an iPhone to this computer
 LPT Pro (Local Photo Transfer) shows what is on the iPhone, what is already
 backed up, and copies what is missing. Every copy is verified. It never
 changes anything on the phone and never uses the internet.
CTRL
cat > "$PKG/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
exit 0
POST
chmod 755 "$PKG/DEBIAN/postinst"
fakeroot dpkg-deb --build "$PKG" "$OUT/" 2>/dev/null || dpkg-deb --root-owner-group --build "$PKG" "$OUT/"
echo "Built $OUT/lpt-pro_${VERSION}_${ARCH}.deb"
