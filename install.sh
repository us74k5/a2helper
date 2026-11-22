#!/usr/bin/env bash
# Installs AION 2 Helper (the macro and the cube overlay) for your user on Linux.
#
#   ./install.sh                 install or update
#   ./install.sh --skip-system   skip the steps that need your password
#   ./install.sh --uninstall     remove it (your settings and calibration are kept)
#
# Works on Bazzite, Fedora and Ubuntu. Needs python3 (3.10 or newer); Bazzite ships it.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/aion2-helper"
APP="$DATA/app"
VENV="$DATA/venv"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
HELPER_DIR=/usr/local/libexec/aion2-helper
me="${USER:-$(id -un)}"
skip_system=0 uninstall=0

for arg in "$@"; do
  case "$arg" in
    --skip-system) skip_system=1 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
say()  { printf '\n\033[1;36m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not with sudo"

if [ "$uninstall" = 1 ]; then
  pkill -f "python.* -m aion2helper" 2>/dev/null || true
  rm -rf "$APP" "$VENV" "$DATA/upstream" "$BIN/aion2-helper" "$APPS/aion2-helper.desktop"
  if [ -d "$HELPER_DIR" ]; then sudo rm -rf "$HELPER_DIR"; fi
  echo "Removed. Your settings and map calibration are still in ~/.config/aion2-helper (delete that folder to remove them)."
  echo "The one-time system setup (input group, uinput rule) was left in place; it is harmless."
  exit 0
fi

command -v python3 >/dev/null || die "python3 is not installed"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "python3 is too old (3.10 or newer is needed)"
[ -d "$SRC/aion2helper" ] || die "run this from the extracted aion2-helper folder (aion2helper/ must sit next to it)"

say "1/5  Installing the program to $APP"
mkdir -p "$APP" "$BIN" "$APPS"
rm -rf "$APP/aion2helper"
cp -r "$SRC/aion2helper" "$APP/aion2helper"
find "$APP" -name '__pycache__' -prune -exec rm -rf {} +

say "2/5  Python packages (Qt for the overlay, lz4 for the parser; the first time downloads ~200 MB)"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import pip' >/dev/null 2>&1; then
  # Bazzite can replace the system Python during an image update; rebuild the environment then.
  python3 -m venv --clear "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$SRC/requirements.txt"

say "3/5  Downloading the cube parser (Aion2CubeGPS)"
PYTHONPATH="$APP" "$VENV/bin/python" -m aion2helper fetch-parser ||
  warn "could not download it now; the program tries again when it starts (or press Update parser on its page)"

say "4/5  System permissions"
need=""
getent group input | grep -qw "$me" || need="$need group"
[ -e /etc/udev/rules.d/99-aion2-helper-uinput.rules ] || [ -e /etc/udev/rules.d/99-uinput.rules ] || need="$need uinput"
# the installed copies must match this version (the helper's first line is its shebang)
if ! cmp -s <(tail -n +2 "$SRC/aion2helper/capture/linux_helper.py") <(tail -n +2 "$HELPER_DIR/aion2-capture" 2>/dev/null) ||
   ! cmp -s "$SRC/aion2helper/capture/core.py" "$HELPER_DIR/core.py"; then need="$need capture"; fi
if [ -z "$need" ]; then
  echo "already set up"
elif [ "$skip_system" = 1 ]; then
  warn "skipped (--skip-system). Still needed:$need"
else
  command -v sudo >/dev/null || die "sudo is needed for this step"
  echo "This needs your password once (it sets up:$need)."
  tmp="$(mktemp -d)"
  # The capture helper runs as root for a moment, so it gets its own root-owned copy that runs
  # with the system Python in isolated mode. Nothing in your home folder ever runs as root.
  { echo '#!/usr/bin/python3 -I'; tail -n +2 "$SRC/aion2helper/capture/linux_helper.py"; } > "$tmp/aion2-capture"
  cp "$SRC/aion2helper/capture/core.py" "$tmp/core.py"
  sudo env USER_NAME="$me" NEED="$need" TMPD="$tmp" HELPER_DIR="$HELPER_DIR" sh -c '
    set -e
    case "$NEED" in *capture*)
      install -d -o root -g root -m 0755 "$HELPER_DIR"
      install -o root -g root -m 0755 "$TMPD/aion2-capture" "$HELPER_DIR/aion2-capture"
      install -o root -g root -m 0644 "$TMPD/core.py" "$HELPER_DIR/core.py" ;; esac
    case "$NEED" in *group*)
      # image-based systems (Bazzite) keep the input group in /usr/lib/group
      if ! grep -q "^input:" /etc/group && grep -q "^input:" /usr/lib/group 2>/dev/null; then grep "^input:" /usr/lib/group >> /etc/group; fi
      usermod -aG input "$USER_NAME" ;; esac
    case "$NEED" in *uinput*)
      echo "KERNEL==\"uinput\", GROUP=\"input\", MODE=\"0660\", OPTIONS+=\"static_node=uinput\"" > /etc/udev/rules.d/99-aion2-helper-uinput.rules
      echo uinput > /etc/modules-load.d/aion2-helper-uinput.conf
      modprobe uinput; udevadm control --reload; udevadm trigger ;; esac'
  rm -rf "$tmp"
  case "$need" in *group*) RELOG=1 ;; esac
fi

say "5/5  Menu entry"
cat > "$BIN/aion2-helper" <<EOF
#!/bin/sh
export PYTHONPATH="$APP"
exec "$VENV/bin/python" -m aion2helper "\$@"
EOF
chmod +x "$BIN/aion2-helper"
cat > "$APPS/aion2-helper.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AION 2 Helper
Comment=AION 2 macro helper and hidden cube overlay
Exec=$BIN/aion2-helper
Icon=applications-games
Terminal=false
Categories=Game;
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

say "Done."
echo "Open 'AION 2 Helper' from your applications menu (or run: aion2-helper). A page opens in your browser."
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN is not on your PATH; run $BIN/aion2-helper" ;; esac
old=""
[ -e "$BIN/aion-cube" ] && old="$old the cube overlay (aion-cube-linux/uninstall.sh),"
[ -e "$BIN/combat-helper" ] && old="$old Combat Helper (./install.sh --uninstall in its folder),"
[ -n "$old" ] && warn "The old programs are still installed:${old%,}. Remove them so they don't run alongside this one; your calibration and keys were copied over."
if [ "${RELOG:-0}" = 1 ]; then warn "You were just added to the 'input' group: log out and back in once before the first Start."; fi
