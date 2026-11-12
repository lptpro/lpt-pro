# SPDX-License-Identifier: GPL-3.0-or-later
# PyInstaller recipe for LPT Pro.   Build with:  pyinstaller packaging/lptpro.spec
# Output: dist/LPTPro/ (one folder; Windows: LPTPro.exe, Linux: LPTPro)
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
WIN = sys.platform == "win32"

hidden = collect_submodules("pymobiledevice3") + collect_submodules("lptpro")
datas = [(os.path.join(ROOT, "lptpro", f), "lptpro") for f in ("icon.png", "icon.ico", "video.qml")]
datas += collect_data_files("pymobiledevice3")
if not WIN:
    hidden += collect_submodules("pillow_heif")

# Qt parts the app doesn't use (keeps the download small)
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtWebSockets", "PySide6.QtHttpServer",
    "tkinter", "matplotlib", "IPython", "pytest",
    "av",   # video library pulled in by pymobiledevice3, not used here
]
if WIN:
    excludes.append("pillow_heif")

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
    noarchive=False,
)
# Drop Qt Quick modules and styles the app doesn't load (it only uses QtQuick + QtMultimedia
# for the video view) and Qt's translations (the app's own text is English).
import re
_DROP = re.compile(r"(Quick3D|Qt3D|/QtCharts|/QtGraphs|Charts|Graphs|DataVisualization|Qt5Compat|"
                   r"QtWebEngine|WebEngine|Pdf|Wayland(?!.*platforms)|LabsStyleKit|QtLocation|"
                   r"QuickControls2(FluentWinUI3|Imagine|Material|Universal|Fusion|Basic|"
                   r"Windows|MacOS|IOS)|QuickDialogs2|ShaderTools|VirtualKeyboard|Scxml|"
                   r"RemoteObjects|Sensors|Positioning|Bluetooth|Nfc|SerialPort|Spatial|TextToSpeech|"
                   r"/translations/)", re.I)
def _keep(entry):
    return not _DROP.search(entry[0].replace("\\", "/"))
a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="LPTPro",
    console=False,
    icon=os.path.join(ROOT, "lptpro", "icon.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="LPTPro", upx=False)
