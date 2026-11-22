# PyInstaller recipe. From the project folder:  pyinstaller packaging/aion2-helper.spec
import sys

windows = sys.platform == "win32"

# The cube parser is downloaded at run time and imported then, so PyInstaller cannot see what it
# needs. These are its imports (besides the ones it gets stubbed out).
parser_needs = ["argparse", "collections", "dataclasses", "json", "math", "os", "struct", "sys",
                "threading", "time", "typing", "lz4", "lz4.block"]

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    datas=[("../aion2helper/web", "web")],
    hiddenimports=parser_needs + ["aion2helper.input_windows" if windows else "aion2helper.input_linux"],
    excludes=["tkinter", "unittest", "test", "evdev",
              # Qt parts the overlay does not use (keeps the download smaller)
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
              "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtNetwork"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="aion2-helper",
          console=not windows,  # Windows: no console window; the page is the interface
          upx=False,            # compressed executables are one of the things antivirus programs distrust
          version="windows/version_info.txt" if windows else None)
coll = COLLECT(exe, a.binaries, a.datas, name="aion2-helper")
