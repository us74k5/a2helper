# Third-party software

AION 2 Helper is licensed under the GNU General Public License v3.0 (see `LICENSE`).
The Windows build includes the following software, each under its own license. The full license
texts are in the `licenses` folder.

| Software | Used for | License | Text |
|---|---|---|---|
| [Qt for Python (PySide6, Shiboken6) and Qt 6](https://www.qt.io/qt-for-python) | the overlay windows | LGPL-3.0 | `licenses/LGPL-3.0.txt` (with `LICENSE`, the GPL-3.0 it builds on) |
| [Python](https://www.python.org/) | the runtime | PSF License | `licenses/Python-PSF.txt` |
| [python-lz4](https://github.com/python-lz4/python-lz4) | decompressing game packets | BSD-3-Clause | `licenses/python-lz4-BSD-3-Clause.txt` |
| [LZ4](https://github.com/lz4/lz4) (inside python-lz4) | the same | BSD-2-Clause | `licenses/lz4-library-BSD-2-Clause.txt` |
| [PyInstaller](https://pyinstaller.org/) bootloader | starting the packaged program | GPL-2.0-or-later with the Bootloader Exception | `licenses/PyInstaller.txt` |

**Qt / PySide6 (LGPL-3.0).** These libraries are used unmodified and are kept as separate files
in the program's `_internal` folder, so you can replace them with your own build of the same
version. Their source code is available from the Qt Project at <https://code.qt.io/> (Qt) and
<https://code.qt.io/cgit/pyside/pyside-setup.git/> (PySide6), and on request from the maintainer of
this repository.

## Not included

- **Npcap** (Windows packet capture) is not bundled: its free license does not allow it. Players
  install it themselves from <https://npcap.com/>.
- **Aion2CubeGPS's parser** (`working_aion_radar.py`, the hidden-cube packet decoding) is not
  bundled: its license has not been established. The program downloads it to the player's own
  computer from <https://github.com/fokeiro2020/Aion2CubeGPS>.
