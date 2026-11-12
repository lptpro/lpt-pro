# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point:  python -m lptpro             (real iPhone)
                 python -m lptpro --check     (connection test in the console)
                 python -m lptpro --demo DIR  (use a folder as a pretend phone)"""
import argparse
import logging
import os
import sys

from PySide6.QtCore import QStandardPaths, QCoreApplication
from PySide6.QtWidgets import QApplication

from . import (APP_ID, APP_LONG_NAME, SETTINGS_NAME, __version__, migrate_from_test_builds,
               settings)
from .device import AfcBackend, FolderBackend
from .ui import STYLE, MainWindow


def main():
    ap = argparse.ArgumentParser(description=f"{APP_LONG_NAME} {__version__}")
    ap.add_argument("--demo", metavar="FOLDER", help="use a local folder as a pretend iPhone")
    ap.add_argument("--check", action="store_true", help="test the iPhone connection in the console, no window")
    args = ap.parse_args()

    if args.check:
        return check(args.demo)

    QCoreApplication.setOrganizationName(SETTINGS_NAME)
    QCoreApplication.setApplicationName(SETTINGS_NAME)
    migrate_from_test_builds()
    _apply_video_engine()
    if sys.platform == "win32":
        # Own taskbar button + icon when run from source (otherwise Windows groups it under
        # python.exe). A Store (MSIX) install already has its own identity.
        try:
            import ctypes
            n = ctypes.c_uint32(0)
            packaged = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(n), None) != 15700
            if not packaged:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass
    app = QApplication(sys.argv)
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "icon.png")))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)

    data = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    os.makedirs(data, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(os.path.join(data, "lptpro.log"), encoding="utf-8"),
                  logging.StreamHandler()],
    )
    logging.getLogger("transfer").info("starting %s", __version__)
    _log_video_engine()
    _apply_gpu_choice()   # before any window or video creates a graphics device
    logging.getLogger("transfer").info(
        "video engine: %s, decoding on: %s", os.environ.get("QT_MEDIA_BACKEND", "ffmpeg"),
        "processor" if os.environ.get("QT_FFMPEG_DECODING_HW_DEVICE_TYPES") == "none" else "graphics card")

    device = FolderBackend(args.demo) if args.demo else AfcBackend()
    win = MainWindow(device)
    win.show()
    sys.exit(app.exec())


GPU_VALUES = {"power": "GpuPreference=1;", "performance": "GpuPreference=2;"}


def _apply_gpu_choice():
    """Windows: which graphics chip this app uses (player ⋯ menu; applies from next start).
    "auto" = let Windows decide (default). The app only ever removes values it set itself."""
    if sys.platform != "win32":
        return
    log = logging.getLogger("transfer")
    st = settings()
    choice = st.value("gpu_choice", "auto")
    try:
        import ctypes
        import winreg

        buf = ctypes.create_unicode_buffer(32768)
        ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))   # the real python(w).exe
        exes = {buf.value}
        for p in (sys.executable, getattr(sys, "_base_executable", "")):
            if p:
                d = os.path.dirname(p)
                exes |= {os.path.join(d, "python.exe"), os.path.join(d, "pythonw.exe")}
        ours = set(GPU_VALUES.values())   # values this app may have written itself
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\DirectX\UserGpuPreferences")
        with key:
            for exe in sorted(e for e in exes if os.path.isfile(e)):
                try:
                    current, _ = winreg.QueryValueEx(key, exe)
                except FileNotFoundError:
                    current = None
                want = GPU_VALUES.get(choice)
                if current is not None and current not in ours:
                    log.info("GPU: %s has the user's own setting (%s); leaving it", exe, current)
                elif want and current != want:
                    winreg.SetValueEx(key, exe, 0, winreg.REG_SZ, want)
                    log.info("GPU: %s -> %s", exe, choice)
                elif not want and current is not None:
                    winreg.DeleteValue(key, exe)
                    log.info("GPU: %s -> let Windows decide", exe)
    except Exception:
        log.exception("could not apply the GPU choice")


def _apply_video_engine():
    """Windows: "ffmpeg" (Qt's FFmpeg, default) or "windows" (Media Foundation). Must be set
    before Qt's multimedia starts."""
    if sys.platform != "win32":
        return
    st = settings()
    if st.value("video_engine", "ffmpeg") == "windows":
        os.environ["QT_MEDIA_BACKEND"] = "windows"
    # Decode video on the processor (multi-threaded FFmpeg) by default.
    # Handing hardware-decoded frames from the decoder to the drawing side is a weak spot in
    # Qt; a name Qt doesn't know ("none") leaves it no hardware decoder to try.
    if st.value("video_decoding", "software") == "software":
        os.environ["QT_FFMPEG_DECODING_HW_DEVICE_TYPES"] = "none"


def _log_video_engine():
    """Send Qt's video-decoding notes to lptpro.log, so we can see whether the
    graphics card (hardware decoding) is being used when playback isn't smooth."""
    from PySide6.QtCore import QLoggingCategory, QtMsgType, qInstallMessageHandler

    qlog = logging.getLogger("transfer.qt")
    QLoggingCategory.setFilterRules("qt.multimedia.ffmpeg.hwaccel*=true\n"
                                    "qt.multimedia.playbackengine.codec*=true\n"
                                    "qt.rhi.general=true")   # which graphics chip draws the window

    seen = set()

    def handler(mode, ctx, msg):
        if "pipewire" in msg:
            return
        key = (ctx.category, msg)
        if key in seen:          # each video tile repeats the same decoder lines: log once
            return
        seen.add(key)
        level = logging.WARNING if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg) else logging.INFO
        qlog.log(level, "%s: %s", ctx.category or "qt", msg)

    qInstallMessageHandler(handler)


def check(demo=None):
    """Console self-test: connect, count files, try reading a photo. Read-only."""
    import time
    from .media import scan_phone
    dev = FolderBackend(demo) if demo else AfcBackend()
    if not demo:
        from . import setup_check
        for step in setup_check.check():
            print("SETUP NEEDED:", step.title)
            print("  " + step.text)
            for label, kind, payload in step.actions:
                if kind == "copy":
                    print("  Command:", payload)
                elif kind == "url":
                    print(f"  {label}: {payload}")
            return 1
    print("Connecting… (unlock the iPhone and tap Trust if asked)")
    try:
        dev.connect()
    except Exception as e:
        print("FAILED:", e)
        return 1
    print(f"Connected: {dev.name}  iOS {dev.ios}  {dev.model}")
    t = time.time()
    items = scan_phone(dev, progress=lambda m: print("  " + m, end="\r"))
    print()
    from .jobs import confirm_cloud_hints
    n_cloud = confirm_cloud_hints(dev, items)
    photos = sum(1 for i in items if i.kind == "photo")
    print(f"Found {len(items):,} items ({photos:,} photos, {len(items) - photos:,} videos) in {time.time() - t:.1f}s")
    print(f"Only in iCloud (originals not on the iPhone): {n_cloud:,}")
    if items:
        it = items[0]
        print(f"Newest: {it.name}  {it.date:%Y-%m-%d %H:%M}  {it.size:,} bytes")
        print("Fingerprint of its first 64 KB (read normally, worked out on the PC):", dev.head_hash(it.path, it.size).hex())
    heics = [i for i in items if i.ext in (".heic", ".heif")]
    if heics:
        from . import heic
        it = heics[0]
        try:
            t = time.time()
            img, info = heic.decode(dev.read_all(it.path))
            jpg = heic.to_jpeg(dev.read_all(it.path))
            print(f"HEIC photos: OK ({heic.backend_name()}) - {it.name} is {img.width}x{img.height}, "
                  f"JPEG {len(jpg) // 1024:,} KB, {time.time() - t:.1f}s")
            img2, nread = heic.fast_thumbnail(dev, it.path, it.size)
            if img2 is not None:
                print(f"HEIC tile shortcut: OK - built-in preview {img2.width}x{img2.height}, "
                      f"read {nread // 1024:,} KB of {it.size // 1024:,} KB")
            else:
                print("HEIC tile shortcut: NOT USED - tiles will read whole photos (slower)")
        except heic.HeicUnavailable as e:
            print("HEIC photos: NOT AVAILABLE -", e)
        except Exception as e:
            print(f"HEIC photos: FAILED ({type(e).__name__}: {e})")
    try:
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: F401
        print("Video player: available")
    except Exception as e:
        print(f"Video player: NOT AVAILABLE ({e})")
    dev.close()
    print("All good.")
    return 0


if __name__ == "__main__":
    main()
