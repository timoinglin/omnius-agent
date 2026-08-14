#!/usr/bin/env python3
"""Omnius desktop verbs - screen-read + a CLOSED registry of named actions.

    python tools\\desktop\\desktop.py windows
    python tools\\desktop\\desktop.py screenshot [--window "Visual Studio Code"]
    python tools\\desktop\\desktop.py focus "Visual Studio Code"
    python tools\\desktop\\desktop.py open notepad
    python tools\\desktop\\desktop.py key ctrl+s
    python tools\\desktop\\desktop.py type-into notepad "hello"
    python tools\\desktop\\desktop.py close notepad

THE REGISTRY IS THE ALLOWLIST (user decision 2026-08-01). There is deliberately
no `click x y`, no `run <command>`, no `open <arbitrary path>`. Every action is
a named function in VERBS below, and adding one is a git commit somebody can
read - self-improvement with receipts, ARCHITECTURE par. 3.6.

Why not raw pyautogui driven from chat: this system's entire safety model is the
Claude Code permission layer - scoped allow-lists, absolute `deny` entries, and
the #alerts escalation hook (docs\\PERMISSIONS.md). Unrestricted GUI automation
defeats all of it at once. It can click "Allow" on any permission dialog, type
into any window, and read .env off the screen, which turns every `deny` from
absolute into advisory. That matters more than usual here: a second instance of
this system runs on employer hardware against a company Discord server, and the
threat model already says "whoever can write in these channels can drive your
PC". Named verbs keep the chokepoint meaningful while still giving real remote
control.

Dependencies: none beyond Pillow (already present, used only to encode the PNG).
Windows itself is driven through ctypes/user32 - pyautogui is not needed for
normal Win32 apps, and pydirectinput would only be for DirectInput targets
(games, some anti-cheat apps). Do not take either dependency speculatively.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "state" / "logs"
SHOTS = ROOT / "media" / "sent"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

# Declare every prototype. ctypes defaults an undeclared return type to C int
# (32-bit), which silently TRUNCATES a 64-bit HWND - so GetForegroundWindow()
# would compare unequal to the same window's handle and the focus check would
# report failure (or worse, success against a truncated handle). It usually
# works, because handles are usually small, which is exactly what makes it a
# heisenbug worth removing rather than living with.
user32.GetForegroundWindow.restype = wt.HWND
user32.GetFocus.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.BringWindowToTop.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
user32.SendInput.restype = ctypes.c_uint
kernel32.GetCurrentThreadId.restype = wt.DWORD

SW_RESTORE = 9
WM_CLOSE = 0x0010
DWMWA_CLOAKED = 14
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_MENU = 0x12

VERBS = {}

# Verbs that inject input can confirm that WINDOWS ACCEPTED the events, and
# nothing more. Measured 2026-08-01 against the Store-app Notepad: SendInput
# returned the full count and the audit log recorded "ok" for every `key` call,
# while the app did nothing at all - Ctrl+A/Ctrl+C left a clipboard sentinel
# untouched with the target confirmed in the foreground. Reporting that as
# success is the worst failure mode a remote-control verb can have, because the
# person trusting it is not in the room. So these verbs say what they know, and
# the honest confirmation is the read side: take a screenshot and look.
UNVERIFIED = ("delivery NOT verified (the OS accepted the events; whether the app "
              "acted on them is unknown - confirm with `screenshot`)")

# Verbs safe to expose over Discord. `key`/`type-into` are deliberately absent:
# they are the highest-risk verbs AND the only ones whose effect this module
# cannot confirm, which is a bad combination to hand to a chat message. They
# stay available from the local CLI, where a human can see the screen.
REMOTE_VERBS = ("windows", "screenshot", "focus", "open", "close")


def verb(name, help_text):
    def deco(fn):
        fn.help = help_text
        VERBS[name] = fn
        return fn
    return deco


# --- audit trail --------------------------------------------------------------

def audit(verb_name, detail, caller, result):
    """Every invocation, in one place, forever.

    A remote-control surface without a log is a surface nobody can review after
    the fact. Note what is NOT recorded: the literal text of `type-into`. It
    would be the obvious place for a password to end up in a file that is not
    treated as a secret (workspace rule: secrets live only in root .env)."""
    line = json.dumps({"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                       "verb": verb_name, "detail": detail,
                       "caller": caller, "result": result})
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        p = LOGS / "desktop.log"
        if p.exists() and p.stat().st_size > 1_000_000:
            p.replace(LOGS / "desktop.log.1")
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --- window enumeration -------------------------------------------------------

def _cloaked(hwnd):
    """DWM-cloaked windows are invisible but still 'visible' to user32: UWP
    ghosts, 'Windows Input Experience', background store apps. Without this the
    window list is mostly noise the user has never seen on screen."""
    val = ctypes.c_int(0)
    try:
        dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), ctypes.c_uint(DWMWA_CLOAKED),
                                     ctypes.byref(val), ctypes.sizeof(val))
    except OSError:
        return False
    return bool(val.value)


def list_windows():
    """-> [{hwnd, title, pid}] for real, visible, titled top-level windows."""
    found = []
    CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if _cloaked(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append({"hwnd": int(hwnd), "title": buf.value, "pid": int(pid.value)})
        return True

    user32.EnumWindows(CB(cb), 0)
    return found


def find_window(needle):
    """One window by case-insensitive substring, or a ValueError that says why.

    Ambiguity is an ERROR, never a guess. Picking 'the first match' for a verb
    that then types into it is how remote control types a message into the wrong
    window - the failure this whole design exists to make unlikely."""
    needle = (needle or "").strip().lower()
    if not needle:
        raise ValueError("no window given")
    wins = list_windows()
    exact = [w for w in wins if w["title"].lower() == needle]
    hits = exact or [w for w in wins if needle in w["title"].lower()]
    if not hits:
        raise ValueError(f"no visible window matches {needle!r} - try the `windows` verb")
    if len(hits) > 1:
        titles = ", ".join(repr(w["title"]) for w in hits[:6])
        raise ValueError(f"{len(hits)} windows match {needle!r}: {titles} - be more specific")
    return hits[0]


# --- input synthesis ----------------------------------------------------------

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTunion)]


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput sent {sent}/{len(inputs)} events")


def _vk_event(vk, up=False):
    ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0,
                    time=0, dwExtraInfo=None)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=ki))


def _char_event(ch, up=False):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    ki = KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flags, time=0, dwExtraInfo=None)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=ki))


# A fixed table, not a parser over arbitrary virtual-key codes: `key` must not
# become a way to synthesise anything at all.
KEYS = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}


def parse_combo(combo):
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    unknown = [p for p in parts if p not in KEYS]
    if unknown:
        raise ValueError(f"unknown key(s): {', '.join(unknown)} - "
                         f"allowed: {', '.join(sorted(KEYS))}")
    return [KEYS[p] for p in parts]


def _focus_hwnd(hwnd):
    """Bring a window to the front, and TELL THE TRUTH about whether it worked.

    Windows refuses SetForegroundWindow from a process that does not own the
    foreground. The trick every StackOverflow answer gives is a stray ALT tap to
    lift the restriction - DO NOT USE IT HERE. Caught live 2026-08-01 by looking
    at the screenshot instead of at the exit code: a lone ALT press-release puts
    a Win32 app into MENU MODE, so `type-into notepad "hola desde Omnius…"` put
    "hola" in the document and then fed the remaining characters to the menu bar
    as access keys, opening the context menu. The verb reported "typed 35 chars"
    and exited 0 the whole time.

    AttachThreadInput shares input state with the current foreground thread,
    which grants the foreground change legitimately and synthesises no keys at
    all. Nothing here may touch the keyboard: this function runs immediately
    before text is typed."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    our_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_tid) and fg_tid != our_tid and \
        bool(user32.AttachThreadInput(our_tid, fg_tid, True))
    try:
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(our_tid, fg_tid, False)
    for _ in range(20):
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return False


# --- the verbs ----------------------------------------------------------------

@verb("windows", "list the visible windows (title + pid)")
def v_windows(args):
    wins = list_windows()
    if args.json:
        return json.dumps(wins, ensure_ascii=False)
    return "\n".join(f"{w['title']}  (pid {w['pid']})" for w in wins) or "no windows"


@verb("screenshot", "capture the screen, or one window with --window")
def v_screenshot(args):
    from PIL import ImageGrab   # imported here so the other verbs work without it
    box, what = None, "screen"
    if args.window:
        w = find_window(args.window)
        # Focus first: this grabs a REGION OF THE SCREEN, so anything sitting on
        # top of the target would otherwise be what gets captured and sent.
        _focus_hwnd(w["hwnd"])
        time.sleep(0.25)
        r = wt.RECT()
        user32.GetWindowRect(w["hwnd"], ctypes.byref(r))
        box = (r.left, r.top, r.right, r.bottom)
        what = w["title"]
    img = ImageGrab.grab(bbox=box)
    dest = SHOTS / datetime.now().strftime("%Y-%m")
    dest.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else dest / (
        datetime.now().strftime("screen-%Y%m%d-%H%M%S") + ".png")
    img.save(out, "PNG")
    return f"{out}|captured {what} ({img.width}x{img.height})"


# `open` takes a NAME from this table, never a path or a command line. An
# `open <anything>` verb would be arbitrary code execution from a chat message,
# which is the one thing the named-verb design exists to prevent. Adding an
# entry here is a git commit.
APPS = {
    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "explorer": ["explorer.exe"],
    "terminal": ["wt.exe"],
    "workspace": ["explorer.exe", str(ROOT)],
}


@verb("open", "launch a known app by name (see the APPS table)")
def v_open(args):
    name = (args.target or "").strip().lower()
    if name not in APPS:
        raise ValueError(f"unknown app {name!r} - allowed: {', '.join(sorted(APPS))}")
    subprocess.Popen(APPS[name], close_fds=True)
    return f"launched {name}"


@verb("focus", "bring a window to the front")
def v_focus(args):
    w = find_window(args.target)
    ok = _focus_hwnd(w["hwnd"])
    if not ok:
        raise RuntimeError(f"could not focus {w['title']!r} - Windows refused the "
                           f"foreground change (another app may be holding it)")
    return f"focused {w['title']!r}"


@verb("key", "focus a window and send it a key combo, e.g. key notepad ctrl+s")
def v_key(args):
    # The window is REQUIRED, and this is not bureaucracy. The first version sent
    # the combo to whatever happened to be in the foreground, which meant one
    # thing when run interactively and something else entirely when run from
    # Discord: `focus` and `key` are separate processes, and focus drifted back
    # to the terminal in between - caught live 2026-08-01, a ctrl+n that landed
    # nowhere. A remote key press with no named target is a key press into an
    # unknown window.
    vks = parse_combo(args.text)          # validate before touching the desktop
    w = find_window(args.target)
    if not _focus_hwnd(w["hwnd"]):
        raise RuntimeError(f"refusing to send keys: could not focus {w['title']!r}, "
                           f"they would go to whatever is in front instead")
    _send(*[_vk_event(v) for v in vks],
          *[_vk_event(v, up=True) for v in reversed(vks)])
    return (f"sent {args.text} to {w['title']!r} - {UNVERIFIED}")


@verb("type-into", "focus a window and type text into it")
def v_type_into(args):
    w = find_window(args.target)
    if not _focus_hwnd(w["hwnd"]):
        raise RuntimeError(f"refusing to type: could not focus {w['title']!r}, "
                           f"the text would go to whatever is in front instead")
    text = args.text or ""
    # One SendInput call per batch, NOT one per character. Caught live
    # 2026-08-01: sending each down/up as its own call let the system treat the
    # key as held and auto-repeat it - "hola desde Omnius - verbo type-into"
    # arrived as "hola mmmmmmmmnius tttttttttttttttto". A single array is
    # delivered as one atomic block, which is also what stops another app's
    # input from interleaving mid-word.
    events = []
    for ch in text:
        events.append(_char_event(ch))
        events.append(_char_event(ch, up=True))
    for i in range(0, len(events), 200):
        _send(*events[i:i + 200])
        time.sleep(0.01)
    return f"sent {len(text)} chars to {w['title']!r} - {UNVERIFIED}"


@verb("close", "ask a window to close (graceful, never a force-kill)")
def v_close(args):
    w = find_window(args.target)
    # WM_CLOSE is the same thing the X button does: the app gets to run its own
    # shutdown and can still put up "save changes?". Deliberately not
    # TerminateProcess - remote control must not be able to discard work.
    user32.PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)
    return f"asked {w['title']!r} to close"


# --- CLI ----------------------------------------------------------------------

def run(verb_name, args):
    """-> (ok, message). Never raises: callers are chat handlers."""
    fn = VERBS.get(verb_name)
    if not fn:
        return False, (f"unknown verb {verb_name!r} - allowed: {', '.join(sorted(VERBS))}")
    # `type-into` carries its payload in .text, which is exactly what must not
    # reach the log; everything else is safe to record verbatim.
    detail = (f"{args.target} ({len(args.text or '')} chars)"
              if verb_name == "type-into"
              else (f"{args.target} {args.text}" if verb_name == "key"
                    else (args.target or args.window or "")))
    try:
        out = fn(args)
        audit(verb_name, detail, args.caller, "ok")
        return True, out
    except Exception as e:
        audit(verb_name, detail, args.caller, f"{type(e).__name__}: {e}")
        return False, f"{type(e).__name__}: {e}"


def main(argv):
    # Window titles are arbitrary user text and routinely contain characters the
    # Windows console (cp1252) cannot encode - a braille blank in a media player
    # title killed the very first run of `windows`. Same fix as api.py/watchdog.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(
        description="Omnius desktop verbs (closed registry - see VERBS in this file)",
        epilog="verbs: " + " · ".join(f"{k} ({v.help})" for k, v in sorted(VERBS.items())))
    p.add_argument("verb", choices=sorted(VERBS))
    p.add_argument("target", nargs="?", help="window title / app name / key combo")
    p.add_argument("text", nargs="?", help="text, for type-into")
    p.add_argument("--window", help="window title, for screenshot")
    p.add_argument("--out", help="output path, for screenshot")
    p.add_argument("--json", action="store_true", help="machine-readable where supported")
    p.add_argument("--caller", default="cli", help="who is asking (recorded in the audit log)")
    args = p.parse_args(argv)
    ok, msg = run(args.verb, args)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
