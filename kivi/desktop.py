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
from typing import NamedTuple
from urllib.parse import urlsplit

VIRTUAL_KEYBOARD_NAME = "kivi-virtual-keyboard"


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5, **kw)


def _spawn(cmd):
    """Start a program fully detached from kivi."""
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# ---------------------------------------------------------------- clipboard

# Types we read back as text, best first; wl-paste's "-t text" picks any of them.
_TEXT_TYPES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")
# Password managers (KeePassXC, ...) tag secrets with this; kivi never reads or republishes them.
PASSWORD_HINT = "x-kde-passwordManagerHint"
_SNAPSHOT_LIMIT = 32 * 1024 * 1024


class Snapshot(NamedTuple):
    """What the clipboard held before kivi used it. mime None = it was empty."""
    mime: str | None
    data: bytes = b""


def _klipper(method, *args):
    """Call Klipper over D-Bus in-process (text never goes through a command line)."""
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    params = GLib.Variant("(" + "s" * len(args) + ")", args) if args else None
    res = bus.call_sync("org.kde.klipper", "/klipper", "org.kde.klipper.klipper", method, params,
                        None, Gio.DBusCallFlags.NONE, 5000, None)
    out = res.unpack() if res is not None else ()
    return out[0] if out else None


def _has_klipper():
    if not shutil.which("qdbus6") and "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        return False
    try:
        import gi  # noqa: F401
        return True
    except ImportError:
        return False


class Clipboard:
    """wl-clipboard if installed, else KDE Klipper over D-Bus, else xclip.

    Text is always handed over on stdin (or D-Bus), never in argv: argv is world-readable in
    /proc and limited to 128 KiB per argument.
    """

    def _backend(self):
        if shutil.which("wl-paste") and shutil.which("wl-copy"):
            return "wl"
        if _has_klipper():
            return "klipper"
        if shutil.which("xclip"):
            return "xclip"
        raise RuntimeError("no clipboard tool: install wl-clipboard")

    def types(self) -> list[str]:
        """MIME types currently offered ([] if empty or unknown)."""
        if self._backend() != "wl":
            return []
        p = subprocess.run(["wl-paste", "-l"], capture_output=True, text=True, timeout=5)
        return [t.strip() for t in p.stdout.splitlines() if t.strip()] if p.returncode == 0 else []

    def get(self) -> str:
        """The clipboard as text ("" if it holds no text)."""
        b = self._backend()
        if b == "wl":
            p = subprocess.run(["wl-paste", "-n", "-t", "text"], capture_output=True, timeout=5)
            return p.stdout.decode("utf-8", errors="replace") if p.returncode == 0 else ""
        if b == "klipper":
            return _klipper("getClipboardContents") or ""
        p = subprocess.run(["xclip", "-o", "-sel", "clip"], capture_output=True, timeout=5)
        return p.stdout.decode("utf-8", errors="replace") if p.returncode == 0 else ""

    def set(self, text: str) -> None:
        b = self._backend()
        if b == "wl":
            # wl-copy forks a server that keeps the pipes open; don't capture its output.
            subprocess.run(["wl-copy"], input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=True)
        elif b == "klipper":
            _klipper("setClipboardContents", text)
        else:
            subprocess.run(["xclip", "-sel", "clip"], input=text.encode("utf-8"), timeout=5, check=True)

    def clear(self) -> None:
        b = self._backend()
        if b == "wl":
            subprocess.run(["wl-copy", "--clear"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        elif b == "klipper":
            _klipper("clearClipboardContents")
        else:
            self.set("")

    def snapshot(self) -> Snapshot | None:
        """Save the clipboard so it can be put back; None = don't touch it afterwards
        (a password-manager secret, or something kivi can't read faithfully)."""
        b = self._backend()
        if b != "wl":
            text = self.get()
            return Snapshot("text/plain;charset=utf-8", text.encode("utf-8")) if text else None
        types = self.types()
        if not types:
            return Snapshot(None)
        if PASSWORD_HINT in types:
            return None
        mime = next((t for t in types if t == "text/uri-list"), None)       # copied files
        mime = mime or next((t for t in types if t.startswith("image/")), None)
        mime = mime or next((t for t in _TEXT_TYPES if t in types), None)
        mime = mime or next((t for t in types if "/" in t), None)
        if mime is None:
            return None
        p = subprocess.run(["wl-paste", "-n", "-t", mime], capture_output=True, timeout=5)
        if p.returncode != 0 or len(p.stdout) > _SNAPSHOT_LIMIT:
            return None
        return Snapshot(mime, p.stdout)

    def restore(self, snap: Snapshot) -> None:
        if snap.mime is None:
            self.clear()
        elif snap.mime in _TEXT_TYPES or self._backend() != "wl":
            self.set(snap.data.decode("utf-8", errors="replace"))  # wl-copy offers all text aliases
        else:
            subprocess.run(["wl-copy", "-t", snap.mime], input=snap.data, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=True)

    def safe_get(self) -> str | None:
        try:
            return self.get()
        except Exception:
            return None

    def safe_snapshot(self) -> Snapshot | None:
        try:
            return self.snapshot()
        except Exception:
            return None

    def safe_restore(self, snap: Snapshot) -> None:
        try:
            self.restore(snap)
        except Exception as e:
            print(f"kivi: couldn't restore the clipboard: {e}")


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


class TypingError(RuntimeError):
    """The text is on the clipboard but kivi couldn't press the paste keys."""


def _keyboard(kbd):
    return kbd() if callable(kbd) and not hasattr(kbd, "combo") else kbd


def _why(e):
    msg = str(e)
    if "uinput" in msg.lower() or isinstance(e, PermissionError):
        return "no /dev/uinput access"
    return msg[:120] or e.__class__.__name__


def insert_text(kbd, clip, text, paste_keys="ctrl+v", restore=True, restore_delay=0.6) -> None:
    """Paste `text` into the focused app via the clipboard, then put the old clipboard back.

    `kbd` is a Keyboard or a function returning one; it is only created after the text is
    on the clipboard, so a missing /dev/uinput still leaves the text ready to paste by hand.
    """
    snap = clip.safe_snapshot() if restore else None
    clip.set(text)
    try:
        keyboard = _keyboard(kbd)
        time.sleep(0.05)
        keyboard.combo(paste_keys)
    except Exception as e:
        raise TypingError(f"copied — press {paste_keys.replace('+', ' + ')} "
                          f"(kivi can't type: {_why(e)})") from e
    if snap is not None:
        time.sleep(max(0.1, float(restore_delay)))  # let the target app read the clipboard first
        if clip.safe_get() == text:  # skip if the user (or app) copied something new meanwhile
            clip.safe_restore(snap)


def selected_text(kbd, clip, copy_keys="ctrl+c", wait=0.8) -> str:
    """Copy the current selection with `copy_keys` and return it ("" if nothing is selected).

    The user's clipboard is restored afterwards, so a following insert_text() saves and
    restores the real clipboard rather than the selection.
    """
    snap = clip.safe_snapshot()
    sentinel = f"kivi-{uuid.uuid4().hex}"
    clip.set(sentinel)
    text = None
    try:
        keyboard = _keyboard(kbd)
        time.sleep(0.05)
        keyboard.combo(copy_keys)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:  # slow apps (big browser pages) copy late
            time.sleep(0.05)
            text = clip.safe_get()
            if text is not None and text != sentinel:
                break
    finally:
        if snap is not None:
            clip.safe_restore(snap)
        elif clip.safe_get() == sentinel:
            try:
                clip.clear()
            except Exception:
                pass
    return "" if text is None or text == sentinel else text


# ---------------------------------------------------------------- notifications

_notify_id = None


def notify(summary, body="", timeout_ms=2500) -> None:
    global _notify_id
    if not shutil.which("notify-send"):
        return
    cmd = ["notify-send", "-a", "kivi", "-i", "kivi", "-p", "-t", str(timeout_ms),
           "-h", "string:x-kde-origin-name:kivi", "-h", "boolean:transient:true"]
    if _notify_id:
        cmd += ["-r", _notify_id]
    try:
        out = _run(cmd + ["--", summary, body]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    _notify_id = out or _notify_id


# ---------------------------------------------------------------- launching

ALLOWED_URL_SCHEMES = ("http", "https", "mailto")


def safe_url(url) -> str:
    """Normalise a URL and refuse anything but http(s) and mailto (no file:, smb:, custom handlers)."""
    url = (url or "").strip()
    if not url or any(ord(c) < 32 for c in url):
        raise ValueError("not a valid link")
    if "://" not in url and not url.lower().startswith("mailto:"):
        url = "https://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise ValueError(f"kivi only opens http, https and mailto links (not {scheme or '?'}:)")
    if scheme in ("http", "https") and not parts.netloc:
        raise ValueError("that link has no host")
    return url


def open_url(url) -> None:
    _spawn(["xdg-open", safe_url(url)])


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
        # No PATH/absolute-path fallback on purpose: only installed desktop apps can be launched.
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
