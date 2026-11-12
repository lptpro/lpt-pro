# Third-party software

LPT Pro is built on these open-source projects. Packaged releases include their license
texts in a `licenses` folder next to the program.

| Component | Used for | License |
|---|---|---|
| Python | the language LPT Pro is written in | PSF License |
| Qt 6 / PySide6 | windows, video playback | LGPL-3.0 |
| FFmpeg (bundled with Qt Multimedia) | video decoding | LGPL-2.1-or-later |
| pymobiledevice3 | talking to the iPhone over USB | GPL-3.0 |
| Pillow | reading and writing JPEG / image details | MIT-CMU (HPND) |
| pillow-heif (Linux only) | HEIC photos | BSD-3-Clause; libheif LGPL-3.0 |
| psutil | the app's own CPU and memory check in the log | BSD-3-Clause |
| chardet | text encoding detection (used by a dependency) | LGPL-2.1-or-later |

On Windows, HEIC photos are decoded by Windows itself (the HEIF Image Extensions from
Microsoft). Nothing from it is included in LPT Pro.

The full list of every package in a release, with its license, is generated when the
release is built (`licenses/THIRD-PARTY-LICENSES.txt`).
