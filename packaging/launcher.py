# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point used by the packaged builds (PyInstaller)."""
import sys

from lptpro.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
