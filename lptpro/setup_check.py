# SPDX-License-Identifier: GPL-3.0-or-later
"""First-time setup check: is the iPhone USB service there and running on this system?

Windows: Apple's "Apple Mobile Device Service" (comes with the free Apple Devices app)
listens on 127.0.0.1:27015.
Linux:   the free "usbmuxd" service listens on the /var/run/usbmuxd socket. It is started
when an iPhone is plugged in, which doesn't happen if it was installed while the phone was
already connected, so it can also be started from here.

Returns plain-language steps for whatever is missing; nothing is changed automatically.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class Step:
    title: str
    text: str
    actions: list = field(default_factory=list)   # [(button label, kind, payload)]
    # kinds: "copy" (put payload on the clipboard), "url" (open), "run" (start a command)


def _windows_service_listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 27015), timeout=0.5):
            return True
    except OSError:
        return False


def _windows_service_state() -> str:
    """'running', 'stopped' or 'missing'."""
    try:
        out = subprocess.run(["sc", "query", "Apple Mobile Device Service"], capture_output=True,
                             text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return "missing"
    if out.returncode != 0:
        return "missing"
    return "running" if "RUNNING" in out.stdout else "stopped"


def _linux_usbmuxd_installed() -> bool:
    return bool(shutil.which("usbmuxd")) or any(
        os.path.exists(p) for p in ("/usr/sbin/usbmuxd", "/usr/bin/usbmuxd", "/sbin/usbmuxd"))


def _linux_usbmuxd_running() -> bool:
    path = "/var/run/usbmuxd"
    if not os.path.exists(path):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(path)
        s.close()
        return True
    except OSError:
        return False


def check() -> list[Step]:
    """What still needs doing before an iPhone can connect (empty list = all set)."""
    if sys.platform == "win32":
        if _windows_service_listening():
            return []
        state = _windows_service_state()
        if state == "stopped":
            return [Step("Apple’s iPhone service is installed but not running",
                         "Restart the PC (the service starts with Windows), or open the "
                         "Apple Devices app once. The app will connect by itself as soon as it’s running.")]
        return [Step("Install Apple’s free iPhone driver",
                     "Windows needs Apple’s device service to talk to an iPhone over USB. It comes with "
                     "the free “Apple Devices” app from the Microsoft Store (iTunes is not needed). Install "
                     "it, then plug in your iPhone. LPT Pro will connect by itself.",
                     [("Open the Microsoft Store", "url", "ms-windows-store://pdp/?productid=9NP83LWLPZ9K"),
                      ("Apple’s web page", "url", "https://support.apple.com/en-us/108758")])]
    if sys.platform.startswith("linux"):
        if not _linux_usbmuxd_installed():
            return [Step("Install the iPhone USB service (usbmuxd)",
                         "Linux needs the free usbmuxd service to talk to an iPhone over USB. Open a "
                         "terminal, run the command below, then unplug the iPhone and plug it back in.",
                         [("Copy the command", "copy", "sudo apt install usbmuxd")])]
        if not _linux_usbmuxd_running():
            return [Step("Start the iPhone USB service",
                         "usbmuxd is installed but not running (it starts when an iPhone is plugged in, "
                         "so unplugging and replugging also works). Press “Start it” and enter your "
                         "password when asked.",
                         [("Start it", "run", ["pkexec", "systemctl", "start", "usbmuxd"]),
                          ("Copy the command", "copy", "sudo systemctl start usbmuxd")])]
    return []


def trust_step() -> Step:
    return Step("Unlock your iPhone and tap “Trust”",
                "The service is running. Unlock the iPhone (and keep it unlocked for a moment): "
                "it will ask “Trust This Computer?”. Tap Trust and enter the iPhone passcode. "
                "The app connects by itself.")
