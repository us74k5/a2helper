"""End-to-end check of the installed Windows program on a real Windows desktop (the CI runner).

Installs nothing itself: run it after the installer. It starts the installed program, then checks
through real Windows calls that:
  * the page answers and the cube parser gets downloaded
  * the overlay windows exist, are visible, stay on top and let clicks through
  * the macro really holds F9 and the left mouse button while the right button is held
    (the right button is pressed with SendInput, read back with GetAsyncKeyState)
  * the on/off key pauses it
  * map layer and calibration mode open and close
  * Quit closes the program and lets every key go
Usage: python tests/windows_smoke.py <path to aion2-helper.exe> [screenshot.png]
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

EXE = Path(sys.argv[1])
SHOT = sys.argv[2] if len(sys.argv) > 2 else None
CFG = Path(os.environ["APPDATA"]) / "aion2-helper"
u32 = ctypes.WinDLL("user32", use_last_error=True)
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)


# ── raw input, the same way a person's mouse and keyboard look to the program ──
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def send(inp):
    n = u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    if n != 1:
        raise OSError(f"SendInput failed: {ctypes.get_last_error()}")


def mouse(flag):
    send(INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, flag, 0, 0)))


def key(vk, up=False):
    send(INPUT(type=1, ki=KEYBDINPUT(vk, 0, 2 if up else 0, 0, 0)))


def down(vk):
    return bool(u32.GetAsyncKeyState(vk) & 0x8000)


VK_LBUTTON, VK_RBUTTON, VK_F9, VK_SUBTRACT = 0x01, 0x02, 0x78, 0x6D


# ── the page API ──
def api(port, token, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def windows_of(pid):
    out = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t

    def cb(hwnd, _):
        p = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u32.IsWindowVisible(hwnd):
            r = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            ex = u32.GetWindowLongPtrW(hwnd, -20)  # GWL_EXSTYLE
            out.append({"rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
                        "topmost": bool(ex & 0x8), "clickthrough": bool(ex & 0x20), "layered": bool(ex & 0x80000)})
        return True
    u32.EnumWindows(EnumProc(cb), 0)
    return out


def wait(pred, timeout, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def main() -> int:
    check("installed program exists", EXE.is_file(), EXE)
    proc = subprocess.Popen([str(EXE), "--no-browser"])
    lock = CFG / "running.json"
    check("program started (lock file written)", wait(lock.exists, 30), lock)
    port = json.loads(lock.read_text())["port"]
    html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
    token = re.search(r'const TOKEN = "([^"]+)"', html).group(1)
    check("page served with a token", token and "AION 2 Helper" in html)
    try:
        api(port, "wrong", "/api/state")
        check("wrong token refused", False)
    except urllib.error.HTTPError as e:
        check("wrong token refused", e.code == 403, e.code)

    st = wait(lambda: (s := api(port, token, "/api/state"))["parser"]["present"] and s, 60)
    check("cube parser downloaded on first start", st and st["parser"]["present"])
    st = api(port, token, "/api/state")
    o = st["overlay"]
    print("overlay status:", o["status"], "| capturing:", o["capturing"], flush=True)
    check("overlay reports its capture state", o["enabled"] and o["status"], o["status"])
    npcap = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "Npcap" / "wpcap.dll").exists()
    if npcap:
        check("Npcap capture opened adapters", wait(lambda: api(port, token, "/api/state")["overlay"]["capturing"], 15),
              api(port, token, "/api/state")["overlay"]["status"])
    else:
        check("without Npcap, the page says to install it", "Npcap" in o["status"], o["status"])

    wins = wait(lambda: [w for w in windows_of(proc.pid) if w["rect"][2] < 600], 10)
    print("windows:", windows_of(proc.pid), flush=True)
    check("radar and arrow windows shown", len(wins) >= 2, len(wins))
    check("overlay windows stay on top", wins and all(w["topmost"] for w in wins))
    check("overlay windows let clicks through", wins and all(w["clickthrough"] for w in wins))

    # ── the macro, with real input ──
    for vk in (VK_F9, VK_LBUTTON, VK_RBUTTON):
        if down(vk):
            print("note: key already down before the test:", hex(vk))
    api(port, token, "/api/macro/start", {"dry_run": False})
    check("macro starts", api(port, token, "/api/state")["macro_status"]["running"])
    mouse(0x0008)  # right button down
    held = wait(lambda: down(VK_F9) and down(VK_LBUTTON), 2, 0.02)
    check("holding right mouse holds F9 and left mouse", held,
          f"F9={down(VK_F9)} left={down(VK_LBUTTON)} right={down(VK_RBUTTON)}")
    check("page shows it playing", api(port, token, "/api/state")["macro_status"]["active"])
    time.sleep(0.5)
    check("still held half a second later", down(VK_F9) and down(VK_LBUTTON))
    mouse(0x0010)  # right button up
    released = wait(lambda: not down(VK_F9) and not down(VK_LBUTTON), 2, 0.02)
    check("letting go releases F9 and left mouse", released, f"F9={down(VK_F9)} left={down(VK_LBUTTON)}")

    key(VK_SUBTRACT)                 # an instant tap, shorter than the 10 ms poll
    key(VK_SUBTRACT, up=True)
    check("numpad minus pauses the macro (even a very quick tap)", wait(lambda: not api(port, token, "/api/state")["macro_status"]["enabled"], 2))
    mouse(0x0008)
    time.sleep(0.4)
    check("while paused, nothing is pressed", not down(VK_F9) and not down(VK_LBUTTON))
    mouse(0x0010)
    key(VK_SUBTRACT)
    time.sleep(0.06)  # a normal key press
    key(VK_SUBTRACT, up=True)
    check("numpad minus resumes it", wait(lambda: api(port, token, "/api/state")["macro_status"]["enabled"], 2))

    api(port, token, "/api/settings", {"section": "macro", "values": {"attack_mode": "tap", "attack_interval": 0.3}})
    mouse(0x0008)
    taps = 0
    was = False
    end = time.time() + 1.5
    while time.time() < end:
        now = down(VK_LBUTTON)
        if now and not was:
            taps += 1
        was = now
        time.sleep(0.005)
    f9 = down(VK_F9)
    mouse(0x0010)
    check("tap mode taps left mouse on its interval", 3 <= taps <= 7 and f9, f"{taps} taps in 1.5 s, F9 held={f9}")
    api(port, token, "/api/settings", {"section": "macro", "values": {"attack_mode": "hold"}})

    # ── overlay commands ──
    api(port, token, "/api/overlay", {"cmd": "map"})
    check("map layer opens", wait(lambda: api(port, token, "/api/state")["overlay"]["map_visible"], 3))
    api(port, token, "/api/overlay", {"cmd": "calibrate"})
    check("calibration mode opens", wait(lambda: api(port, token, "/api/state")["overlay"]["calibrating"], 3))
    big = [w for w in windows_of(proc.pid) if w["rect"][2] >= 600]
    check("calibration layer takes clicks", big and not big[0]["clickthrough"], big)
    if SHOT:
        try:
            from PIL import ImageGrab
            time.sleep(0.5)
            ImageGrab.grab().save(SHOT)
            print("screenshot:", SHOT)
        except Exception as e:
            print("no screenshot:", e)
    api(port, token, "/api/overlay", {"cmd": "calibrate"})
    check("calibration saves and closes", wait(lambda: not api(port, token, "/api/state")["overlay"]["calibrating"], 3))

    # ── quit ──
    api(port, token, "/api/quit", {})
    try:
        proc.wait(15)
        check("Quit closes the program", True, f"exit code {proc.returncode}")
    except subprocess.TimeoutExpired:
        proc.kill()
        check("Quit closes the program", False)
    check("lock file removed", not lock.exists())
    check("no key left down", not down(VK_F9) and not down(VK_LBUTTON))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    log = Path(os.environ["LOCALAPPDATA"]) / "aion2-helper" / "aion2-helper.log"
    if log.exists():
        print("\n--- program log ---\n" + log.read_text(encoding="utf-8", errors="replace"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
