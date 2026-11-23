# SPDX-License-Identifier: GPL-3.0-or-later
"""LPT Pro – Local Photo Transfer: copy photos and videos from an iPhone to a PC."""

__version__ = "0.23.2"
APP_NAME = "LPT Pro"
APP_LONG_NAME = "LPT Pro – Local Photo Transfer"
APP_ID = "io.github.lptpro.LPTPro"

SETTINGS_NAME = "LPTPro"          # QSettings organization + application name
BACKUP_META_DIR = ".lptpro"       # hidden folder inside the backup folder
TEMP_DIR_NAME = ".lptpro-tmp"     # unfinished copies (removed when a copy ends)

# names used by test builds before the public release; read once and moved over
_OLD_SETTINGS_NAME = "iPhoneTransfer"
_OLD_BACKUP_META_DIR = ".iphone-transfer"
_OLD_TEMP_DIR_NAME = ".iphone-transfer-tmp"


def settings():
    from PySide6.QtCore import QSettings
    return QSettings(SETTINGS_NAME, SETTINGS_NAME)


def migrate_from_test_builds() -> None:
    """Carry settings and app data over from test builds that used the old internal name."""
    import os
    import shutil
    from PySide6.QtCore import QSettings, QStandardPaths
    new = settings()
    if not new.allKeys():
        old = QSettings(_OLD_SETTINGS_NAME, _OLD_SETTINGS_NAME)
        for k in old.allKeys():
            new.setValue(k, old.value(k))
        new.sync()
    data = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    old_data = data.replace(SETTINGS_NAME, _OLD_SETTINGS_NAME)
    if old_data != data and os.path.isdir(old_data) and not os.path.exists(data):
        try:
            os.makedirs(os.path.dirname(data), exist_ok=True)
            shutil.move(old_data, data)
        except OSError:
            pass
