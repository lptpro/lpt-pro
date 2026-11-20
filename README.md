<p align="center"><img src="lptpro/icon.png" width="128" alt=""></p>

<h1 align="center">LPT Pro</h1>
<p align="center"><b>Local Photo Transfer</b> – copy the photos and videos from your iPhone to your PC.<br>
Free and open source, for Windows and Linux.</p>

<p align="center">
  <a href="https://github.com/lptpro/lpt-pro/releases/latest/download/LPT-Pro-Windows.zip"><b>⬇ Download for Windows</b></a>
  &nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="https://github.com/lptpro/lpt-pro/releases/latest/download/LPT-Pro-Linux.deb"><b>⬇ Download for Linux</b></a>
</p>

<p align="center"><img src="docs/screenshot-main.png" width="900" alt="LPT Pro showing the photos on an iPhone, which ones are already on the PC, and the Back up what's missing button"></p>

---

## What it does

- Shows everything on your iPhone in a fast grid, grouped by the day it was taken.
- Tells you exactly what is **already backed up** on your PC and what isn't, by checking
  the files themselves (renamed or re-sorted copies still count).
- **Back up what's missing** with one button, sorted into folders by year and month.
- Every copy is **verified** before it counts. A file that doesn't match is thrown away
  and reported, never kept half-copied.
- Keeps the original dates and photo details. HEIC photos can be saved as JPEG.
- Plays videos and opens photos full size, straight from the phone.
- Recognises photos in the iPhone's **Recently Deleted** album and leaves them out by
  default, so a pile of deleted screenshots doesn't get backed up.
- Is honest about photos whose full-size original is **only in iCloud** (the iPhone
  setting "Optimize iPhone Storage"), so you never think something is backed up when it isn't.

## What it never does

- It **never changes or deletes anything on your iPhone**. It only reads.
- It never deletes or overwrites anything on your PC.
- It doesn't sync, doesn't need an account and doesn't send anything over the internet.
  Your photos go from the cable to your disk, nowhere else.

## What you need

| | Windows 10 / 11 | Linux |
|---|---|---|
| iPhone USB service | The free **Apple Devices** app from the [Microsoft Store](https://apps.microsoft.com/detail/9NP83LWLPZ9K) | **usbmuxd** (`sudo apt install usbmuxd`; installed for you by the .deb) |
| iTunes | **Not required, and not recommended** | – |
| Cable | A USB cable to the iPhone | same |

The first time you connect, unlock the iPhone and tap **Trust** when it asks
"Trust This Computer?". LPT Pro shows what's missing and connects by itself once
everything is in place.

## Install

### Windows

- **Microsoft Store** (recommended, coming soon): install it like any other app. Store
  apps are checked and signed by Microsoft, so Windows trusts them.
- **Installer** (test versions): click **Download for Windows** at the top of this page, unzip it and run
  `LPT-Pro-Setup-<version>.exe`.
  - Python is built in; nothing else to install except Apple Devices, which the installer
    checks for and helps with.
  - Test versions aren't signed yet, so Windows shows "Windows protected your PC": click
    *More info → Run anyway*.
  - On PCs with **Smart App Control** turned on, Windows blocks unsigned test versions
    completely. Use the Store version there.
- **From source** (for testers): download the source code (green **Code** button →
  *Download ZIP*), unzip it, and double-click **`run_windows.bat`**.
  - If Python isn't installed, it offers to install it for you, using the official
    package through winget.
  - It checks for Apple's USB service and opens the Apple Devices page in the Store if
    it's missing.
  - The first run sets things up (a few minutes, once). Then it puts an **LPT Pro**
    icon on your desktop.

### Linux (Ubuntu, Linux Mint, Debian, …)

- **Installer**: click **Download for Linux** at the top of this page, then run this in the
  folder where it was saved (usually Downloads):
  ```
  sudo apt install ./LPT-Pro-Linux.deb
  ```
  This also installs usbmuxd. LPT Pro then appears in your app menu.
- **Flathub** (coming soon): it will show up in Software Manager / GNOME Software / Discover.
- **From source**: `./run_linux.sh` (needs `python3-venv` and `usbmuxd`).

## Good to know

- **"Only in iCloud"**: with *Optimize iPhone Storage* turned on, the iPhone keeps only a
  small preview of many photos and videos, and the original stays in iCloud. LPT Pro shows
  them with that preview, tagged **iCLOUD ONLY** (choose **Show → Only in iCloud** to see
  just those), and tells you before a backup that they'll be skipped (it can only copy what
  is really on the phone). To get the originals, download them from iCloud on the PC, any
  of these ways:
  - **privacy.apple.com → Request a copy of your data → iCloud Photos**: Apple prepares zip
    files of the whole library (takes a few days).
  - **icloud.com/photos**: select up to 1,000 at a time, **Download → Unmodified Original**.
  - On Windows, Apple's free **iCloud for Windows** app.

  Then press **Add downloaded photos…** and pick the folder or the zip files. LPT Pro copies
  them into your backup, sorted by date, checks every copy, skips any already there, and
  marks them **ON PC**. The downloaded files aren't changed; delete them afterwards if you like.
- **Photos from an old phone, a camera or another backup?** *Add downloaded photos…* sorts
  those into your backup the same way.
- **Something failed?** The **⚠ problems** button at the top lists each file in plain
  words. *Try these again* retries them.
- **Logs**: the Problems window has *Open the log*, which opens a technical record that's
  handy when reporting an issue.

## Reporting a problem

Open an [issue](../../issues). Say what you did and what happened, and attach the log if
you can. The log contains file names from your phone but never the photos themselves.

## License

LPT Pro is free software under the [GNU General Public License v3.0 or later](LICENSE).
It uses open-source components listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Apple, iPhone, iCloud and iTunes are trademarks of Apple Inc. LPT Pro is an independent
project and is not affiliated with or endorsed by Apple.
