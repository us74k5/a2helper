# AION 2 Helper

Aion2 Helper program designed to simplify the in game macro and provide an overlay for hidden cubes.

One small program, one page in your browser:

- **Macro.** Hold the right mouse button and it holds your in-game macro key (F9) and your
  basic attack (left mouse) for you, so the macro cancels its swing on every hit. Every class
  uses the game's own macro for its skill chain, so there is nothing to set up per class.
- **Cube overlay.** A radar, a 3D arrow and a map layer that show hidden cubes while you play.

Works on **Windows** and **Linux** (Bazzite, Fedora, Ubuntu).

> Tools that read the game's network traffic or press keys for you break AION 2's rules on
> third-party programs, and players have reported bans for them. Use this at your own risk.
> It never touches the game's files or memory and never sends anything to the game.

## Install

**Windows**
1. Run `AION2Helper-Setup.exe`. No administrator rights needed. SmartScreen may warn that the
   installer is from an unknown publisher (it is unsigned): choose *More info*, then *Run anyway*.
2. For the cube overlay, install **Npcap** from <https://npcap.com/#download> with the default
   options. The installer offers to open that page if you don't have it. (Don't tick
   "Restrict Npcap driver's access to Administrators only".)
3. Run the game and AION 2 Helper the same way: a game running as administrator ignores input
   from a program that is not.

**Linux**
```
tar xzf aion2-helper-linux.tar.gz
cd aion2-helper
./install.sh
```
It asks for your password once: to let it press keys (the `input` group and a uinput rule) and to
install the small capture helper the overlay uses. If it says you were added to the input group, log
out and back in once. `./install.sh --uninstall` removes it.
Play in **Desktop Mode** (Game Mode's compositor won't let anything draw over the game). The
overlay asks for your password each time it starts capturing; that is the capture helper.

**Coming from Combat Helper or the cube overlay?** The first start copies over your keys and your
map calibration. Then uninstall the old ones so they don't run alongside this one (the installer
reminds you on Linux).

## Use

Open **AION 2 Helper** from the Start menu or your applications menu. The page opens in your
browser; opening the program again just brings the page back.

### Macro
1. Put your whole skill chain in the game's own macro, on F9 (or set your macro key on the page).
   Skills that are only usable after another (Chanter's Dark Crush after Spinning Strike, for
   example) go in the macro right after their opener: the game skips them until they're castable.
2. **Unbind the right mouse button in the game**, or the game's own rotation fires as well.
3. Press **Start**, then hold right mouse to play. Numpad minus pauses and resumes the macro.
   **Test run** writes to the log what it would press, and presses nothing.

The basic attack can be **held** (the default and the point of the helper: the macro cancels its
swing on every hit, which is where the extra damage and mana come from), **tapped on an interval**
(if your macro needs the key to itself), or **left alone**. *Start delay* keeps a quick right-click
to turn the camera from firing the macro.

### Cube overlay
It starts with the program (switch it off on the page if you don't want it). Set the game to
**borderless windowed**. The game only sends your position when you use a skill or dash, so do
that once after logging in.

- **Arrow:** points at the nearest cube relative to the way you're moving, with the distance and
  how far up or down it is. Move a couple of meters and it lines up with your view.
- **Radar:** north up, with distances and a line to the nearest cube.
- **Map layer:** cube markers over the in-game map, once you've calibrated it.

**Hotkeys** (change them on the page): Numpad + shows or hides the map layer, Numpad * calibrates.
Radar and arrow keys are off by default. The game still sees these keys, so pick ones you don't use.

**Calibrate the map once:** open the in-game map at your usual zoom, press the calibrate hotkey,
left-click the tip of your character's arrow, press Enter. Fly 50 m or more (diagonally if you
can), do it again. After two clicks the markers line up. If your map stays centered on you, it
switches to follow mode by itself: scroll until a cube marker sits on its cube. While calibrating:
right-drag or arrow keys move the layer, the wheel scales (Shift = fine), R rotates, F mirrors,
M switches fixed/follow, Backspace clears the clicks.

## Troubleshooting

- **The macro does nothing (Linux):** log out and back in once after the install, then check the
  page has no red warning. `aion2-helper devices` lists the keyboards and mice it can see.
- **The macro does nothing (Windows):** run the game and the helper the same way (both normal, or
  both as administrator).
- **Overlay hidden behind the game:** borderless windowed, not fullscreen (and Desktop Mode on Linux).
- **"Waiting for your position":** use a skill or dash. In crowded places, enter your character
  name on the page.
- **Cubes stopped showing after a game patch:** press **Update parser** on the page.
- Settings: `~/.config/aion2-helper/config.json` on Linux, `%APPDATA%\aion2-helper\config.json` on
  Windows. Log: `~/.local/share/aion2-helper/aion2-helper.log`, `%LOCALAPPDATA%\aion2-helper\aion2-helper.log`.

## How it works

- The page is served on 127.0.0.1 only, and every request carries a secret token, so other
  websites and other computers cannot drive it.
- **Keys:** Windows uses SendInput (scan codes) and reads the trigger with GetAsyncKeyState.
  Linux creates a virtual keyboard and mouse through uinput and reads your devices read-only
  (nothing is grabbed), so it works under Proton on X11 and Wayland. No compiled modules.
- **Capture:** Linux opens one raw socket in a tiny root-owned helper
  (`/usr/local/libexec/aion2-helper`), which drops root right away, before it loads anything from
  your home folder. Windows reads the traffic through Npcap. Only TCP ports 13328 and 13320 are
  looked at, and nothing is ever sent.
- **Cube decoding** is the [Aion2CubeGPS](https://github.com/fokeiro2020/Aion2CubeGPS) project's
  work. Its license has not been established, so this program does not ship it: it downloads that
  one file to your computer (first start, or **Update parser**).

## For whoever builds it

```
pip install -r requirements.txt
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -t .
python -m aion2helper                 # run from source (Linux also needs ./install.sh once for permissions)
```
The GitHub workflow **Build** (Actions tab) makes `AION2Helper-Setup.exe` and
`aion2-helper-linux.tar.gz`; pushing a tag like `v3.0.0` attaches both to a release.

## License

GPL-3.0 (see `LICENSE`). The Windows build bundles Qt/PySide6 (LGPL-3.0), Python, lz4 and
PyInstaller's bootloader; see `THIRD-PARTY-NOTICES.md` and the `licenses` folder.
