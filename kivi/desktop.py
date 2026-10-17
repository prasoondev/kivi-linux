"""Desktop glue: clipboard, virtual keyboard, notifications, launching apps, capability checks."""

import configparser
import difflib
import glob
import os
import shutil
import subprocess
import threading
import time
import uuid

VIRTUAL_KEYBOARD_NAME = "kivi-virtual-keyboard"


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5, **kw)


def _spawn(cmd):
    """Start a program fully detached from kivi."""
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# ---------------------------------------------------------------- clipboard

class Clipboard:
    """wl-clipboard if installed, else KDE Klipper over D-Bus, else xclip."""

    def get(self) -> str:
        if shutil.which("wl-paste"):
            return _run(["wl-paste", "-n"]).stdout
        if shutil.which("qdbus6"):
            return _run(["qdbus6", "org.kde.klipper", "/klipper", "getClipboardContents"]).stdout.removesuffix("\n")
        if shutil.which("xclip"):
            return _run(["xclip", "-o", "-sel", "clip"]).stdout
        raise RuntimeError("no clipboard tool: install wl-clipboard")

    def set(self, text: str) -> None:
        if shutil.which("wl-copy"):
            # wl-copy forks a server that keeps the pipes open; don't capture its output.
            subprocess.run(["wl-copy", "--", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        elif shutil.which("qdbus6"):
            _run(["qdbus6", "org.kde.klipper", "/klipper", "setClipboardContents", text])
        elif shutil.which("xclip"):
            subprocess.run(["xclip", "-sel", "clip"], input=text, text=True, timeout=5)
        else:
            raise RuntimeError("no clipboard tool: install wl-clipboard")

    def safe_get(self) -> str | None:
        try:
            return self.get()
        except Exception:
            return None


# ---------------------------------------------------------------- virtual keyboard

_KEY_ALIASES = {
    "ctrl": "LEFTCTRL", "control": "LEFTCTRL", "shift": "LEFTSHIFT", "alt": "LEFTALT",
    "super": "LEFTMETA", "meta": "LEFTMETA", "win": "LEFTMETA",
    "enter": "ENTER", "return": "ENTER", "esc": "ESC", "del": "DELETE", "ins": "INSERT",
}


class Keyboard:
    """Virtual uinput keyboard used to send paste/copy shortcuts to the focused window."""

    def __init__(self):
        from evdev import UInput, ecodes
        self._e = ecodes
        self._ui = UInput(name=VIRTUAL_KEYBOARD_NAME)
        self._lock = threading.Lock()
        time.sleep(0.3)  # give the compositor a moment to pick up the new device

    def _code(self, name):
        name = name.strip()
        key = "KEY_" + _KEY_ALIASES.get(name.lower(), name.upper().removeprefix("KEY_"))
        code = getattr(self._e, key, None)
        if code is None:
            raise ValueError(f"unknown key {name!r}")
        return code

    def combo(self, spec: str) -> None:
        keys = [self._code(k) for k in spec.split("+") if k.strip()]
        with self._lock:
            for k in keys:
                self._ui.write(self._e.EV_KEY, k, 1)
                self._ui.syn()
                time.sleep(0.01)
            for k in reversed(keys):
                self._ui.write(self._e.EV_KEY, k, 0)
                self._ui.syn()
                time.sleep(0.01)

    def close(self) -> None:
        self._ui.close()


def insert_text(kbd, clip, text, paste_keys="ctrl+v", restore=True) -> None:
    """Paste `text` into the focused app via the clipboard, then put the old clipboard back."""
    old = clip.safe_get() if restore else None
    clip.set(text)
    time.sleep(0.05)
    kbd.combo(paste_keys)
    if restore and old is not None:
        time.sleep(0.4)  # let the target app read the clipboard first
        clip.set(old)


def selected_text(kbd, clip) -> str:
    """Copy the current selection with ctrl+c and return it ("" if nothing is selected).

    The user's clipboard is restored afterwards, so a following insert_text() saves and
    restores the real clipboard rather than the selection.
    """
    old = clip.safe_get()
    sentinel = f"kivi-{uuid.uuid4().hex}"
    clip.set(sentinel)
    time.sleep(0.05)
    kbd.combo("ctrl+c")
    time.sleep(0.25)
    text = clip.safe_get() or ""
    if old is not None:
        clip.set(old)
    return "" if text == sentinel else text


# ---------------------------------------------------------------- notifications

_notify_id = None


def notify(summary, body="", timeout_ms=2500) -> None:
    global _notify_id
    if not shutil.which("notify-send"):
        return
    cmd = ["notify-send", "-a", "kivi", "-i", "kivi", "-p", "-t", str(timeout_ms),
           "-h", "string:x-kde-origin-name:kivi"]
    if _notify_id:
        cmd += ["-r", _notify_id]
    try:
        out = _run(cmd + ["--", summary, body]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    _notify_id = out or _notify_id


# ---------------------------------------------------------------- launching

def open_url(url) -> None:
    url = url.strip()
    if "://" not in url and not url.startswith(("mailto:", "file:")):
        url = "https://" + url
    _spawn(["xdg-open", url])


def _application_dirs():
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs = [data_home, *data_dirs, os.path.expanduser("~/.local/share/flatpak/exports/share"),
            "/var/lib/flatpak/exports/share"]
    return list(dict.fromkeys(os.path.join(d, "applications") for d in dirs if d))


def _desktop_entries():
    """Yield (desktop_id, path, fields) for launchable applications; earlier dirs win."""
    seen = set()
    for base in _application_dirs():
        for path in sorted(glob.glob(os.path.join(base, "**", "*.desktop"), recursive=True)):
            desktop_id = os.path.relpath(path, base).replace(os.sep, "-")
            if desktop_id in seen:
                continue
            seen.add(desktop_id)
            parser = configparser.RawConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                entry = dict(parser["Desktop Entry"])
            except (configparser.Error, KeyError, UnicodeDecodeError, OSError):
                continue
            if (entry.get("type", "Application") != "Application" or entry.get("nodisplay") == "true"
                    or entry.get("hidden") == "true" or not entry.get("exec")):
                continue
            yield desktop_id, path, entry


def _score(query, desktop_id, entry):
    q = query.lower().strip()
    name = entry.get("name", "").lower()
    exe = os.path.basename(entry.get("exec", "").split()[0]).lower() if entry.get("exec") else ""
    extra = " ".join(entry.get(k, "") for k in ("genericname", "keywords", "comment")).lower()
    stem = desktop_id.lower().removesuffix(".desktop").rsplit(".", 1)[-1]
    if q in (name, exe, stem) or q.replace(" ", "") in (name.replace(" ", ""), stem):
        return 1.0
    if name.startswith(q) or exe.startswith(q):
        return 0.9
    if q in name or q in exe or q in stem:
        return 0.8
    if q in extra:
        return 0.6
    return max(difflib.SequenceMatcher(None, q, name).ratio(), difflib.SequenceMatcher(None, q, exe).ratio()) * 0.7


def find_app(name) -> tuple[str, str] | None:
    """Best matching (desktop_id, path) for a spoken app name, or None."""
    best, best_score = None, 0.58  # fuzzy-only matches need a ratio of ~0.83+
    for desktop_id, path, entry in _desktop_entries():
        s = _score(name, desktop_id, entry)
        if s > best_score:
            best, best_score = (desktop_id, path), s
    return best


def launch_app(name) -> bool:
    if not name or not name.strip():
        return False
    match = find_app(name)
    try:
        if match:
            desktop_id, path = match
            if shutil.which("gtk-launch"):
                _spawn(["gtk-launch", desktop_id])
            elif shutil.which("kioclient"):
                _spawn(["kioclient", "exec", path])
            else:
                return False
            return True
        exe = shutil.which(name.strip().lower())
        if exe:
            _spawn([exe])
            return True
    except OSError:
        pass
    return False


# ---------------------------------------------------------------- capability checks

def input_access_ok() -> bool:
    try:
        import evdev
        from evdev import ecodes
    except ImportError:
        return False
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        try:
            keys = dev.capabilities().get(ecodes.EV_KEY, [])
            if dev.name != VIRTUAL_KEYBOARD_NAME and ecodes.KEY_A in keys:
                return True
        finally:
            dev.close()
    return False


def uinput_ok() -> bool:
    return os.access("/dev/uinput", os.R_OK | os.W_OK)


_TYPELIB_DIRS = ["/usr/lib/girepository-1.0", "/usr/lib64/girepository-1.0",
                 "/usr/local/lib/girepository-1.0", "/usr/lib/x86_64-linux-gnu/girepository-1.0"]
_LAYER_SHELL_LIBS = ["/usr/lib/libgtk4-layer-shell.so", "/usr/lib/libgtk4-layer-shell.so.0",
                     "/usr/lib64/libgtk4-layer-shell.so.0", "/usr/lib/x86_64-linux-gnu/libgtk4-layer-shell.so.0"]


def layer_shell_lib() -> str | None:
    """Path of the gtk4-layer-shell shared library (for LD_PRELOAD), or None."""
    return next((p for p in _LAYER_SHELL_LIBS if os.path.exists(p)), None)


def layer_shell_available() -> bool:
    dirs = [d for d in os.environ.get("GI_TYPELIB_PATH", "").split(":") if d] + _TYPELIB_DIRS
    typelib = any(os.path.exists(os.path.join(d, "Gtk4LayerShell-1.0.typelib")) for d in dirs)
    return typelib and layer_shell_lib() is not None
