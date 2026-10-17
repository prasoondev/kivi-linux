"""kivi settings: the Adw manager app (`kivi settings`).

Every change is saved with config.save_config() and followed by an IPC "reload" so the running
daemon picks it up. Sibling modules are imported defensively: the app keeps working (with the
affected bits disabled) when the daemon is down or a core module fails.

Test hooks: KIVI_SETTINGS_PAGE=<page> opens a page directly (home, api-key, shortcuts, voice,
style, memory, history, agent); KIVI_SETTINGS_DIALOG=capture also opens the shortcut capture dialog.
"""

import copy
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

APP_ID = "ai.kivi.Linux"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _import(name):
    try:
        return importlib.import_module(f"{__package__ or 'kivi'}.{name}")
    except Exception as e:  # a sibling being broken must not take the settings app down
        print(f"kivi settings: module {name} unavailable: {e}", file=sys.stderr)
        return None


config = _import("config")
ipc = _import("ipc")
sarvam = _import("sarvam")
audio = _import("audio")
desktop = _import("desktop")
hotkeys = _import("hotkeys")
theme = _import("theme")
VERSION = getattr(sys.modules.get(__package__ or "kivi"), "__version__", "0.2.0")


def _safe(fn, *args, default=None, **kwargs):
    if fn is None:
        return default
    try:
        return fn(*args, **kwargs)
    except Exception:
        traceback.print_exc()
        return default


def run_bg(work, done=None):
    """Run work() on a thread; done(result, error) back on the GTK thread."""
    def target():
        try:
            res, err = work(), None
        except Exception as e:
            res, err = None, e
        if done:
            GLib.idle_add(lambda: (done(res, err), False)[1])
    threading.Thread(target=target, daemon=True).start()


# ---------------------------------------------------------------- static data

FALLBACK_DEFAULTS = {
    "language": "unknown", "cleanup": True, "style": "", "paste_keys": "ctrl+v", "copy_keys": "ctrl+c",
    "restore_clipboard": True, "min_hold": 0.35, "hands_free": True, "history": True,
    "notify": False, "agent_speak": True, "tts_speaker": "shubh", "tts_language": "en-IN",
    "agent_allow_shell": False,
    "shortcuts": {"dictate": "RIGHTCTRL", "command": "RIGHTCTRL+RIGHTSHIFT", "agent": "RIGHTALT"},
    "overlay": {"enabled": True, "position": "bottom", "idle": "pill", "margin": 28},
}

LANGUAGES = [
    ("unknown", "auto-detect"), ("en-IN", "english"), ("hi-IN", "hindi"), ("bn-IN", "bengali"),
    ("gu-IN", "gujarati"), ("kn-IN", "kannada"), ("ml-IN", "malayalam"), ("mr-IN", "marathi"),
    ("od-IN", "odia"), ("pa-IN", "punjabi"), ("ta-IN", "tamil"), ("te-IN", "telugu"),
    ("as-IN", "assamese"), ("ur-IN", "urdu"), ("ne-IN", "nepali"), ("kok-IN", "konkani"),
    ("ks-IN", "kashmiri"), ("sd-IN", "sindhi"), ("sa-IN", "sanskrit"), ("sat-IN", "santali"),
    ("mni-IN", "manipuri"), ("brx-IN", "bodo"), ("mai-IN", "maithili"), ("doi-IN", "dogri"),
]
TTS_LANGUAGES = LANGUAGES[1:12]
TTS_SPEAKERS = getattr(sarvam, "TTS_SPEAKERS", None) or [
    "shubh", "aditya", "ritu", "priya", "neha", "rahul", "pooja", "rohan", "simran", "kavya",
    "amit", "dev", "ishita", "shreya", "ratan", "varun", "manan", "sumit", "roopa", "kabir",
    "aayan", "ashutosh", "advait", "anand", "tanya", "tarun", "sunny", "mani", "gokul", "vijay",
    "shruti", "suhani", "mohit", "kavitha", "rehan", "soham", "rupali",
]
PREVIEW_TEXT = {
    "hi-IN": "नमस्ते, मैं kivi हूँ। आप बोलिए, मैं लिख दूँगा।",
    "en-IN": "hi, i'm kivi. you talk, i type. ask me anything.",
}

PERSONAS = [
    ("developer", "technical and terse. use exact code terms, keep code identifiers in backticks. no corporate fluff."),
    ("messaging", "lowercase. keep my slang. emojis are fine."),
    ("productivity", "use a heading and bullets. keep status scannable."),
]

SHORTCUT_ROWS = [
    ("dictate", "dictate", "hold to talk. let go, kivi types."),
    ("command", "edit & command", "select text, hold, say the change. nothing selected? just ask."),
    ("agent", "voice agent", "hold and ask kivi to do things: open apps, look things up, type for you."),
]
MODE_TAGS = {"dictate": "dictate", "command": "edit", "agent": "agent"}

PAGES = [
    ("home", "home", "go-home-symbolic"),
    ("api-key", "api key", "dialog-password-symbolic"),
    ("shortcuts", "shortcuts", "input-keyboard-symbolic"),
    ("voice", "voice & language", "audio-input-microphone-symbolic"),
    ("style", "style", "document-edit-symbolic"),
    ("memory", "memory", "bookmark-new-symbolic"),
    ("history", "history", "document-open-recent-symbolic"),
    ("agent", "agent", "system-run-symbolic"),
]

FIX_INPUT = "sudo usermod -aG input $USER"
FIX_INPUT_NOW = "sudo setfacl -m u:$USER:rw /dev/input/event*"
FIX_UINPUT = "sudo setfacl -m u:$USER:rw /dev/uinput"
FIX_LAYER_SHELL = "sudo pacman -S --needed gtk4-layer-shell"
FIX_WL_CLIPBOARD = "sudo pacman -S --needed wl-clipboard"

MODIFIERS = {"LEFTCTRL", "RIGHTCTRL", "LEFTSHIFT", "RIGHTSHIFT", "LEFTALT", "RIGHTALT",
             "LEFTMETA", "RIGHTMETA", "COMPOSE", "FN"}
_KEY_LEGENDS = {
    "LEFTMETA": "super", "RIGHTMETA": "right super", "COMPOSE": "menu", "ESC": "esc",
    "SPACE": "space", "CAPSLOCK": "caps lock", "BACKSPACE": "backspace", "ENTER": "enter",
    "TAB": "tab", "LEFT": "←", "RIGHT": "→", "UP": "↑", "DOWN": "↓", "PAGEUP": "page up",
    "PAGEDOWN": "page down", "SYSRQ": "print", "SCROLLLOCK": "scroll lock", "GRAVE": "`",
    "MINUS": "-", "EQUAL": "=", "LEFTBRACE": "[", "RIGHTBRACE": "]", "SEMICOLON": ";",
    "APOSTROPHE": "'", "BACKSLASH": "\\", "COMMA": ",", "DOT": ".", "SLASH": "/",
}


def key_legend(name):
    """evdev name (no KEY_) -> lowercase keycap legend: RIGHTCTRL -> 'right ctrl'."""
    name = name.upper().removeprefix("KEY_")
    if name in _KEY_LEGENDS:
        return _KEY_LEGENDS[name]
    for side in ("LEFT", "RIGHT"):
        if name.startswith(side) and len(name) > len(side):
            return f"{side.lower()} {name[len(side):].lower()}"
    if name.startswith("KP"):
        return "num " + name[2:].lower()
    return name.lower().replace("_", " ")


def combo_label(combo):
    if hotkeys and hasattr(hotkeys, "combo_label"):
        label = _safe(hotkeys.combo_label, combo)
        if label:
            return label
    return " + ".join(key_legend(k) for k in combo.split("+") if k)


def evdev_name(code):
    """evdev key code -> name without KEY_ (None if unknown)."""
    try:
        from evdev import ecodes
    except Exception:
        return None
    name = ecodes.KEY.get(code) or ecodes.BTN.get(code)
    if isinstance(name, (list, tuple)):
        name = next((n for n in name if n.startswith("KEY_")), name[0])
    return name.removeprefix("KEY_") if isinstance(name, str) else None


def time_ago(ts):
    try:
        d = time.time() - float(ts)
    except (TypeError, ValueError):
        return ""
    if d < 45:
        return "just now"
    if d < 90:
        return "a minute ago"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        h = int(d // 3600)
        return f"{h} hour{'s' if h > 1 else ''} ago"
    if d < 172800:
        return "yesterday"
    return time.strftime("%d %b", time.localtime(float(ts))).lower()


def _systemctl(*args, timeout=8):
    if not shutil.which("systemctl"):
        return 127, ""
    try:
        p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _kivi_exe():
    local = os.path.join(REPO_ROOT, "bin", "kivi")
    return shutil.which("kivi") or (local if os.access(local, os.X_OK) else None)


def _ipc(msg, timeout=2.0):
    return _safe(getattr(ipc, "request", None), msg, timeout=timeout)


def _start_buttons_only_icon():
    """True if the window-button layout puts nothing but the app icon on the left."""
    try:
        layout = Gtk.Settings.get_default().get_property("gtk-decoration-layout") or ""
    except Exception:
        return False
    start = layout.split(":", 1)[0] if ":" in layout else ""
    return {p.strip() for p in start.split(",") if p.strip()} <= {"icon"}


# ---------------------------------------------------------------- css

LOCAL_CSS = """
:root {
  --window-bg-color: #0b0e0a;
  --window-fg-color: #f4f1e6;
  --view-bg-color: #0f130d;
  --view-fg-color: #f4f1e6;
  --headerbar-bg-color: #0b0e0a;
  --headerbar-fg-color: #f4f1e6;
  --headerbar-border-color: rgba(255, 255, 255, 0.08);
  --headerbar-backdrop-color: #0b0e0a;
  --headerbar-shade-color: rgba(0, 0, 0, 0);
  --sidebar-bg-color: #0e120c;
  --sidebar-fg-color: #f4f1e6;
  --sidebar-backdrop-color: #0e120c;
  --sidebar-shade-color: rgba(0, 0, 0, 0.25);
  --sidebar-border-color: rgba(255, 255, 255, 0.07);
  --card-bg-color: #121710;
  --card-fg-color: #f4f1e6;
  --card-shade-color: rgba(0, 0, 0, 0.3);
  --dialog-bg-color: #121710;
  --dialog-fg-color: #f4f1e6;
  --popover-bg-color: #1a2016;
  --popover-fg-color: #f4f1e6;
  --accent-bg-color: #a8e063;
  --accent-fg-color: #0b0e0a;
  --accent-color: #a8e063;
  --destructive-bg-color: #ff6b5b;
  --destructive-fg-color: #0b0e0a;
  --destructive-color: #ff6b5b;
  --success-color: #a8e063;
  --warning-color: #c77e28;
  --error-color: #ff6b5b;
  --border-color: rgba(255, 255, 255, 0.10);
}

window.kivi-settings, dialog.kivi-capture {
  font-family: "Switzer Variable", "Switzer", system-ui, sans-serif;
  background-color: #0b0e0a;
  color: #f4f1e6;
}
window.kivi-settings .content-pane { background-color: #0b0e0a; }
row, row label, button, button label, entry, text, banner, toast, popover label {
  font-family: "Switzer Variable", "Switzer", system-ui, sans-serif;
}
headerbar { box-shadow: none; }

/* type */
.kivi-wordmark { font-family: "Fraunces", Georgia, serif; font-size: 24px; font-weight: 500; color: #f4f1e6; letter-spacing: -0.01em; }
.kivi-hero { font-family: "Fraunces", Georgia, serif; font-size: 46px; font-weight: 400; letter-spacing: -0.024em; color: #f4f1e6; }
.kivi-title { font-family: "Fraunces", Georgia, serif; font-size: 32px; font-weight: 400; letter-spacing: -0.016em; color: #f4f1e6; }
.kivi-display-sm { font-family: "Fraunces", Georgia, serif; font-size: 20px; font-weight: 400; color: #f4f1e6; }
.kivi-lede { color: #b9b6a8; font-size: 15px; }
.kivi-eyebrow { color: #7d7b6f; font-size: 11px; font-weight: 600; letter-spacing: 0.1em; }
.kivi-dim { color: #7d7b6f; }
.kivi-ink-2 { color: #b9b6a8; }
.kivi-green { color: #a8e063; }
.kivi-amber { color: #c77e28; }
.kivi-error { color: #ff6b5b; }
.kivi-mono { font-family: "Geist Mono", "JetBrains Mono", "Noto Sans Mono", monospace; }
.kivi-foot { color: #7d7b6f; font-size: 11px; }

/* preference groups: lowercase titles in the eyebrow voice */
preferencesgroup > box > box.header .title { color: #b9b6a8; font-weight: 600; font-size: 13px; }
preferencesgroup > box > box.header .description { color: #7d7b6f; }
row .subtitle { color: #9a988b; }

/* sidebar */
.kivi-sidebar { background: transparent; }
.kivi-sidebar > row { border-radius: 8px; margin: 1px 8px; padding: 9px 10px; color: #b9b6a8; }
.kivi-sidebar > row image { color: #7d7b6f; }
.kivi-sidebar > row:hover:not(:selected) { background: rgba(255, 255, 255, 0.04); color: #f4f1e6; }
.kivi-sidebar > row:selected { background: rgba(168, 224, 99, 0.13); color: #f4f1e6; }
.kivi-sidebar > row:selected image { color: #a8e063; }
.kivi-bar { background: #a8e063; border-radius: 999px; min-width: 2px; }
.kivi-bars.dim .kivi-bar { background: #7d7b6f; }

/* cards, lists */
.kivi-card, list.boxed-list {
  background-color: #121710;
  border-radius: 12px;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.08), 0 4px 24px rgba(10, 12, 8, 0.16);
}
.kivi-card { padding: 18px 20px; }
.kivi-card.tight { padding: 12px 16px; }
list.boxed-list > row { border-color: rgba(255, 255, 255, 0.06); }
row.activatable:hover { background-color: rgba(255, 255, 255, 0.03); }

/* buttons */
button.suggested-action, button.kivi-primary {
  background: #a8e063; color: #0b0e0a; border-radius: 999px; font-weight: 600; padding: 5px 16px;
}
button.suggested-action:hover, button.kivi-primary:hover { background: #b8ea78; }
button.suggested-action:disabled { background: rgba(168, 224, 99, 0.3); color: rgba(11, 14, 10, 0.7); }
button.kivi-ghost {
  border-radius: 999px; padding: 5px 14px; background: rgba(255, 255, 255, 0.06);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.08); color: #f4f1e6;
}
button.kivi-ghost:hover { background: rgba(255, 255, 255, 0.1); }
button.kivi-ghost.small { padding: 2px 12px; min-height: 28px; font-size: 13px; }
button.destructive-action { background: rgba(255, 107, 91, 0.14); color: #ff8a7d; border-radius: 999px; padding: 5px 14px; }
button.destructive-action:hover { background: rgba(255, 107, 91, 0.22); }
switch:checked { background-color: #a8e063; }
switch:checked > slider { background-color: #0b0e0a; }
scale highlight { background-color: #a8e063; }
scale slider { background-color: #f4f1e6; }
link, button.link > label { color: #a8e063; }
entry, text, textview { caret-color: #a8e063; }
selection { background-color: rgba(168, 224, 99, 0.30); color: #f4f1e6; }
spinner { color: #a8e063; }

textview.kivi-textarea, textview.kivi-textarea text { background-color: transparent; color: #f4f1e6; font-size: 15px; }
textview.kivi-textarea { border: none; padding: 0; border-radius: 0; box-shadow: none; }
.kivi-textbox {
  background-color: #0f130d; border-radius: 12px; padding: 14px 16px;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.10);
}
.kivi-textbox:focus-within { box-shadow: inset 0 0 0 1px rgba(168, 224, 99, 0.55), 0 0 0 4px rgba(168, 224, 99, 0.10); }
.kivi-placeholder { color: #5f5e55; font-size: 15px; }

/* status */
.kivi-dot { min-width: 9px; min-height: 9px; border-radius: 999px; background: #7d7b6f; }
.kivi-dot.on { background: #a8e063; box-shadow: 0 0 0 4px rgba(168, 224, 99, 0.16); }
.kivi-dot.warn { background: #c77e28; box-shadow: 0 0 0 4px rgba(199, 126, 40, 0.16); }
.kivi-check { min-width: 22px; min-height: 22px; border-radius: 999px; font-size: 12px; font-weight: 700; }
.kivi-check.ok { background: rgba(168, 224, 99, 0.14); color: #a8e063; }
.kivi-check.bad { background: rgba(255, 107, 91, 0.14); color: #ff6b5b; }
.kivi-check.optional { background: rgba(199, 126, 40, 0.14); color: #e0a35a; }
.kivi-code {
  font-family: "Geist Mono", "JetBrains Mono", "Noto Sans Mono", monospace; font-size: 12px;
  color: #d6efc1; background: rgba(168, 224, 99, 0.07); border-radius: 8px; padding: 7px 10px;
  box-shadow: inset 0 0 0 1px rgba(168, 224, 99, 0.16);
}

/* keycaps: skirt + lighter face + cream mono legend, after heykivi.ai's .keycap */
.keycap {
  font-family: "Geist Mono", "JetBrains Mono", "Noto Sans Mono", monospace;
  font-size: 12px; color: #efeade;
  min-width: 22px; min-height: 18px;
  padding: 5px 10px 8px 10px;
  border-radius: 7px;
  background-image: linear-gradient(176deg, #5a6353 0%, #414937 46%, #333a2e 100%);
  box-shadow: inset 0 1px 0 rgba(246, 243, 234, 0.30),
              inset 0 -3px 0 #22271f,
              inset 0 -4px 0 #171a15,
              0 0 0 1px rgba(12, 14, 12, 0.7),
              0 4px 6px rgba(6, 10, 5, 0.5);
}
.keycap.big { font-size: 15px; padding: 9px 16px 13px 16px; border-radius: 10px; }
.keycap.held {
  background-image: linear-gradient(176deg, #4c5546 0%, #383f30 46%, #2c3228 100%);
  box-shadow: inset 0 1px 0 rgba(246, 243, 234, 0.2),
              inset 0 -2px 0 #171a15,
              0 0 0 2px rgba(168, 224, 99, 0.65),
              0 0 0 7px rgba(168, 224, 99, 0.14);
}
.keycap-plus { color: #7d7b6f; font-size: 12px; }

/* chips & tags */
.kivi-chip {
  padding: 4px 12px; border-radius: 999px;
  background: rgba(255, 255, 255, 0.05); color: #b9b6a8;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.10);
  font-size: 13px; font-weight: 500;
}
button.kivi-chip { min-height: 0; }
button.kivi-chip:hover { background: rgba(168, 224, 99, 0.12); color: #e6f5d4; box-shadow: inset 0 0 0 1px rgba(168, 224, 99, 0.3); }
.kivi-chip.green { background: rgba(168, 224, 99, 0.10); color: #d6efc1; box-shadow: inset 0 0 0 1px rgba(168, 224, 99, 0.24); }
.kivi-word { padding: 2px 4px 2px 12px; border-radius: 999px; background: #1a2016; box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.10); }
.kivi-word label { color: #f4f1e6; font-size: 13px; }
.kivi-word button { min-width: 22px; min-height: 22px; padding: 0; border-radius: 999px; color: #7d7b6f; }
.kivi-word button:hover { color: #ff8a7d; background: rgba(255, 107, 91, 0.12); }
.kivi-tag { padding: 1px 8px; border-radius: 999px; background: rgba(255, 255, 255, 0.06); color: #b9b6a8; font-size: 10px; font-weight: 600; letter-spacing: 0.06em; }
.kivi-tag.dictate { background: rgba(168, 224, 99, 0.12); color: #d6efc1; }
.kivi-tag.command { background: rgba(199, 126, 40, 0.16); color: #f0c592; }
.kivi-tag.agent { background: rgba(37, 107, 47, 0.38); color: #cfeeb0; }
.kivi-said-label { color: #7d7b6f; font-size: 10px; font-weight: 600; letter-spacing: 0.08em; }
.kivi-said { color: #b9b6a8; font-size: 13px; }
.kivi-result { color: #f4f1e6; font-size: 14px; }
.kivi-warning { color: #e0a35a; font-size: 13px; }
.kivi-quote { font-family: "Fraunces", Georgia, serif; font-size: 18px; color: #f4f1e6; }

dialog.kivi-capture .kivi-title { font-size: 26px; }
"""


def apply_css():
    css = (getattr(theme, "SETTINGS_CSS", "") or "") + LOCAL_CSS
    if theme and hasattr(theme, "apply") and _safe(theme.apply, css, default=False) is not False:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(css)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_USER)


# ---------------------------------------------------------------- small widget helpers

def label(text="", *classes, xalign=0.0, wrap=False, markup=False, selectable=False, **kw):
    w = Gtk.Label(xalign=xalign, wrap=wrap, selectable=selectable, **kw)
    if wrap:
        w.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    (w.set_markup if markup else w.set_text)(text)
    for c in classes:
        w.add_css_class(c)
    return w


def vbox(spacing=0, *classes, **kw):
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kw)
    for c in classes:
        b.add_css_class(c)
    return b


def hbox(spacing=0, *classes, **kw):
    b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing, **kw)
    for c in classes:
        b.add_css_class(c)
    return b


def button(text, *classes, on_click=None, icon=None, tooltip=None, valign=Gtk.Align.CENTER):
    b = Gtk.Button(valign=valign)
    if icon and text:
        b.set_child(Adw.ButtonContent(icon_name=icon, label=text))
    elif icon:
        b.set_icon_name(icon)
    else:
        b.set_label(text)
    for c in classes:
        b.add_css_class(c)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if on_click:
        b.connect("clicked", lambda *_: on_click())
    return b


def bars(dim=False, scale=1.0):
    """The kivi glyph: five thin rounded green bars."""
    box = hbox(int(2 * scale), "kivi-bars", valign=Gtk.Align.CENTER)
    if dim:
        box.add_css_class("dim")
    for s in (0.42, 0.78, 0.56, 0.9, 0.64):
        bar = Gtk.Box(valign=Gtk.Align.CENTER)
        bar.add_css_class("kivi-bar")
        bar.set_size_request(max(2, int(2 * scale)), max(3, int(16 * scale * s)))
        box.append(bar)
    return box


def keycaps(combo, big=False):
    box = hbox(6, valign=Gtk.Align.CENTER)
    keys = [k for k in (combo or "").split("+") if k]
    for i, k in enumerate(keys):
        if i:
            box.append(label("+", "keycap-plus"))
        cap = label(key_legend(k), "keycap", xalign=0.5)
        if big:
            cap.add_css_class("big")
        box.append(cap)
    if not keys:
        box.append(label("not set", "kivi-dim"))
    return box


def copy_to_clipboard(text):
    display = Gdk.Display.get_default()
    if display:
        display.get_clipboard().set(text)


def textarea(placeholder, height=120):
    """A rounded multi-line text box with a placeholder; returns (frame, textview)."""
    tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
    tv.add_css_class("kivi-textarea")
    tv.set_size_request(-1, height)
    hint = label(placeholder, "kivi-placeholder", wrap=True, valign=Gtk.Align.START, can_target=False)
    overlay = Gtk.Overlay(child=tv)
    overlay.add_overlay(hint)
    buf = tv.get_buffer()
    buf.connect("changed", lambda b: hint.set_visible(b.get_char_count() == 0))
    frame = vbox(0, "kivi-textbox")
    frame.append(overlay)
    click = Gtk.GestureClick()
    click.connect("pressed", lambda *_: tv.grab_focus())
    frame.add_controller(click)
    return frame, tv


def combo_row(title, subtitle, options, current, on_change):
    values = [v for v, _ in options]
    labels = [lbl for _, lbl in options]
    if current not in values:
        values.append(current)
        labels.append(str(current))
    row = Adw.ComboRow(title=title, subtitle=subtitle or "", model=Gtk.StringList.new(labels))
    row.set_selected(values.index(current))
    row.connect("notify::selected", lambda r, _p: on_change(values[r.get_selected()]))
    return row


def switch_row(title, subtitle, active, on_change):
    row = Adw.SwitchRow(title=title, subtitle=subtitle or "", active=bool(active))
    row.connect("notify::active", lambda r, _p: on_change(r.get_active()))
    return row


def group(title="", description=""):
    return Adw.PreferencesGroup(title=title, description=description)


# ---------------------------------------------------------------- shortcut capture

class CaptureDialog(Adw.Dialog):
    """'press and hold your new shortcut…'; combo = keys held at the first release. Esc cancels."""

    def __init__(self, action_title, on_done, taken):
        super().__init__(title=f"new {action_title} shortcut", content_width=460, content_height=360)
        self.add_css_class("kivi-capture")
        self.on_done, self.taken, self.action_title = on_done, taken, action_title
        self.order, self.held, self.combo = [], set(), None

        body = vbox(18, margin_top=8, margin_bottom=24, margin_start=28, margin_end=28)
        self.heading = label("press and hold your new shortcut…", "kivi-title", wrap=True, xalign=0.5,
                             justify=Gtk.Justification.CENTER)
        body.append(self.heading)
        self.caps = hbox(0, halign=Gtk.Align.CENTER, height_request=64)
        body.append(self.caps)
        self.hint = label("release to finish · esc cancels", "kivi-dim", xalign=0.5, wrap=True)
        body.append(self.hint)
        self.warn = label("", "kivi-warning", xalign=0.5, wrap=True, visible=False,
                          justify=Gtk.Justification.CENTER)
        body.append(self.warn)

        actions = hbox(10, halign=Gtk.Align.CENTER, margin_top=6)
        self.retry = button("try again", "kivi-ghost", on_click=self._reset)
        self.save = button("save shortcut", "suggested-action", on_click=self._save)
        for b in (self.retry, self.save):
            b.set_sensitive(False)
            b.set_focusable(False)
            actions.append(b)
        body.append(actions)

        tb = Adw.ToolbarView(content=body)
        tb.add_top_bar(Adw.HeaderBar(show_title=False))
        self.set_child(tb)
        self._render([])

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._pressed)
        keys.connect("key-released", self._released)
        self.add_controller(keys)
        self.set_focus(None)

    def _render(self, names, held=True):
        while (c := self.caps.get_first_child()):
            self.caps.remove(c)
        if names:
            caps = keycaps("+".join(names), big=True)
            if held:
                for c in list(_children(caps)):
                    if c.has_css_class("keycap"):
                        c.add_css_class("held")
            self.caps.append(caps)
        else:
            self.caps.append(bars(dim=True, scale=2.0))

    def _pressed(self, _ctl, _keyval, keycode, _state):
        if self.combo is not None:
            return False  # captured: let buttons / Enter work normally
        code = keycode - 8
        if code == 1:  # KEY_ESC
            self.close()
            return True
        name = evdev_name(code)
        if not name:
            return True
        if code not in self.held:
            self.held.add(code)
            if name not in self.order:
                self.order.append(name)
            self._render(self.order)
        return True

    def _released(self, _ctl, _keyval, keycode, _state):
        if self.combo is not None or not self.held:
            return
        names = list(self.order)
        mods = [n for n in names if n in MODIFIERS]
        self.combo = "+".join(mods + [n for n in names if n not in MODIFIERS])
        self._render(self.combo.split("+"), held=False)
        self.heading.set_text("got it.")
        self.hint.set_text(f"hold it to {self.action_title}.")
        warning = ""
        if self.combo in self.taken:
            warning = f"already used by {self.taken[self.combo]}. saving will make both start the same mode."
        elif not mods:
            warning = "tip: modifier keys like right ctrl or right alt work best, so normal typing isn't affected."
        if hotkeys and hasattr(hotkeys, "parse_combo"):
            try:
                hotkeys.parse_combo(self.combo)
            except ValueError as e:
                warning = f"kivi can't use this one: {e}"
                self.save.set_visible(False)
        self.warn.set_text(warning)
        self.warn.set_visible(bool(warning))
        for b in (self.retry, self.save):
            b.set_sensitive(True)
            b.set_focusable(True)
        self.save.grab_focus()

    def _reset(self):
        self.order, self.held, self.combo = [], set(), None
        self.heading.set_text("press and hold your new shortcut…")
        self.hint.set_text("release to finish · esc cancels")
        self.warn.set_visible(False)
        self.save.set_visible(True)
        for b in (self.retry, self.save):
            b.set_sensitive(False)
            b.set_focusable(False)
        self._render([])
        self.set_focus(None)

    def _save(self):
        if self.combo:
            self.on_done(self.combo)
        self.close()


def _children(widget):
    c = widget.get_first_child()
    while c:
        yield c
        c = c.get_next_sibling()


# ---------------------------------------------------------------- main window

class SettingsWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="kivi", default_width=900, default_height=640)
        self.add_css_class("kivi-settings")
        self.set_size_request(360, 420)
        self.defaults = copy.deepcopy(getattr(config, "DEFAULTS", None) or FALLBACK_DEFAULTS)
        self.cfg = self._load_cfg()
        self._save_timer = 0
        self._poll_busy = False
        self._refreshers = {}
        self._updating = False

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=180)
        builders = {
            "home": self._page_home, "api-key": self._page_api_key, "shortcuts": self._page_shortcuts,
            "voice": self._page_voice, "style": self._page_style, "memory": self._page_memory,
            "history": self._page_history, "agent": self._page_agent,
        }
        for name, title, _icon in PAGES:
            try:
                page = builders[name]()
            except Exception:
                traceback.print_exc()
                page = self._scaffold(title, "this page couldn't load. check the terminal for details.")[0]
            self.stack.add_named(page, name)

        content_tb = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.FLAT)
        content_tb.add_top_bar(Adw.HeaderBar(show_title=False))
        content_tb.set_content(self.stack)
        content_tb.add_css_class("content-pane")
        self.toasts.set_child(content_tb)
        self.content_page = Adw.NavigationPage(title="home", child=self.toasts, tag="content")

        self.split = Adw.NavigationSplitView(sidebar=self._sidebar(), content=self.content_page,
                                             min_sidebar_width=210, max_sidebar_width=250,
                                             sidebar_width_fraction=0.25)
        self.set_content(self.split)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 620sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self.connect("close-request", self._on_close)
        start = (os.environ.get("KIVI_SETTINGS_PAGE") or "home").strip().lower()
        start = {"api key": "api-key", "apikey": "api-key", "key": "api-key", "voice & language": "voice",
                 "language": "voice"}.get(start, start)
        self.show_page(start if start in builders else "home")
        GLib.timeout_add_seconds(3, self._poll_tick)

    # -- config plumbing
    def _load_cfg(self):
        cfg = copy.deepcopy(self.defaults)
        loaded = _safe(getattr(config, "load_config", None)) or {}
        for k, v in loaded.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        cfg.pop("api_key", None)  # only ever read on demand via get_api_key()
        return cfg

    def get(self, path):
        node = self.cfg
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node

    def set(self, path, value):
        if self._updating:
            return
        *parents, leaf = path.split(".")
        node = self.cfg
        for p in parents:
            node = node.setdefault(p, {})
        if node.get(leaf) == value:
            return
        node[leaf] = value
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(350, self._flush)

    def _flush(self):
        self._save_timer = 0
        if config is None or not hasattr(config, "save_config"):
            self.toast("couldn't save: config module unavailable")
            return False
        try:
            data = copy.deepcopy(self.cfg)
            data.pop("api_key", None)
            config.save_config(data)
        except Exception as e:
            traceback.print_exc()
            self.toast(f"couldn't save settings: {e}")
            return False
        self.reload_daemon()
        return False

    def reload_daemon(self):
        run_bg(lambda: _ipc({"cmd": "reload"}))

    def _on_close(self, *_):
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._flush()
        return False

    def toast(self, text, timeout=3):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout))

    # -- navigation
    def _sidebar(self):
        self.nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.nav.add_css_class("kivi-sidebar")
        self._rows = {}
        for name, title, icon in PAGES:
            row = Gtk.ListBoxRow()
            row.page_name = name
            box = hbox(12)
            box.append(Gtk.Image(icon_name=icon))
            box.append(label(title))
            row.set_child(box)
            self.nav.append(row)
            self._rows[name] = row
        self.nav.connect("row-activated", lambda _l, row: self.show_page(row.page_name))

        brand = hbox(10, margin_start=6)
        brand.append(bars(scale=1.1))
        brand.append(label("kivi", "kivi-wordmark"))
        header = Adw.HeaderBar()
        header.pack_start(brand)
        header.set_title_widget(Gtk.Box())
        if _start_buttons_only_icon():  # e.g. "icon:minimize,maximize,close": don't show a 2nd logo
            header.set_show_start_title_buttons(False)

        body = vbox(0, margin_top=6)
        body.append(Gtk.ScrolledWindow(child=self.nav, vexpand=True,
                                       hscrollbar_policy=Gtk.PolicyType.NEVER))
        body.append(label(f"v{VERSION} · powered by sarvam ai", "kivi-foot",
                          margin_start=20, margin_bottom=16, margin_top=8))
        tb = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.FLAT, content=body)
        tb.add_top_bar(header)
        return Adw.NavigationPage(title="kivi", child=tb, tag="sidebar")

    def show_page(self, name):
        self.stack.set_visible_child_name(name)
        self.content_page.set_title(dict((n, t) for n, t, _ in PAGES)[name])
        row = self._rows[name]
        if self.nav.get_selected_row() is not row:
            self.nav.select_row(row)
        self.split.set_show_content(True)
        if name in self._refreshers:
            _safe(self._refreshers[name])

    def _poll_tick(self):
        if self.get_visible() and self.stack.get_visible_child_name() == "home":
            _safe(self._refresh_status)
        return True

    # -- page scaffold
    def _scaffold(self, title, lede=""):
        box = vbox(28, margin_top=12, margin_bottom=40, margin_start=28, margin_end=28)
        head = vbox(8)
        head.append(label(title, "kivi-title"))
        if lede:
            head.append(label(lede, "kivi-lede", wrap=True))
        box.append(head)
        clamp = Adw.Clamp(maximum_size=680, tightening_threshold=520, child=box)
        scroller = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        return scroller, box

    # ============================================================ 1. home
    def _page_home(self):
        scroller, box = self._scaffold("", "")
        box.remove(box.get_first_child())
        dictate = combo_label(self.get("shortcuts.dictate") or "")

        hero = vbox(12, margin_top=8)
        hero.append(label("you talk. <span foreground='#a8e063'>kivi types.</span>", "kivi-hero",
                          markup=True, wrap=True))
        self.hero_sub = label(f"hold {dictate} anywhere, talk, and let go. polished text lands at your cursor, "
                              "in any app.", "kivi-lede", wrap=True)
        hero.append(self.hero_sub)
        box.append(hero)

        # daemon status
        status = group("kivi on this desktop")
        self.status_row = Adw.ActionRow(title="checking…", subtitle=" ")
        self.status_dot = Gtk.Box(valign=Gtk.Align.CENTER, margin_end=4)
        self.status_dot.add_css_class("kivi-dot")
        self.status_row.add_prefix(self.status_dot)
        self.status_btns = hbox(8, valign=Gtk.Align.CENTER)
        self.btn_start = button("start", "suggested-action", on_click=lambda: self._daemon("start"))
        self.btn_restart = button("restart", "kivi-ghost", on_click=lambda: self._daemon("restart"))
        self.btn_stop = button("stop", "kivi-ghost", on_click=lambda: self._daemon("stop"))
        self.status_spin = Adw.Spinner(visible=False)
        for w in (self.status_spin, self.btn_start, self.btn_restart, self.btn_stop):
            self.status_btns.append(w)
        self.status_row.add_suffix(self.status_btns)
        status.add(self.status_row)
        self.login_row = switch_row("launch at login", "start kivi when you sign in (systemd user service)",
                                    False, self._set_login)
        status.add(self.login_row)
        box.append(status)

        # checklist
        checks = group("setup", "everything kivi needs to listen and type for you.")
        self.check_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.check_list.add_css_class("boxed-list")
        checks.add(self.check_list)
        box.append(checks)

        # try it
        tryit = group("try it", f"click in the box, hold {dictate} and say something.")
        self.try_frame, self.try_view = textarea("you talk. kivi types. try “remind me to call maa at six, "
                                                 "no wait, seven”", height=110)
        tryit.add(self.try_frame)
        box.append(tryit)

        self._refreshers["home"] = self._refresh_home
        return scroller

    def _refresh_home(self):
        self.hero_sub.set_text(f"hold {combo_label(self.get('shortcuts.dictate') or '')} anywhere, talk, and "
                               "let go. polished text lands at your cursor, in any app.")
        self._refresh_status()
        run_bg(self._probe_checks, self._render_checks)

    def _refresh_status(self):
        if self._poll_busy:
            return
        self._poll_busy = True

        def probe():
            st = _ipc({"cmd": "status"}, timeout=1.0)
            _, active = _systemctl("is-active", "kivi")
            _, enabled = _systemctl("is-enabled", "kivi")
            return st, active, enabled

        run_bg(probe, self._render_status)

    def _render_status(self, res, err):
        self._poll_busy = False
        st, active, enabled = res if res else (None, "", "")
        running = isinstance(st, dict) and st.get("ok", True)
        self.status_dot.remove_css_class("on")
        self.status_dot.remove_css_class("warn")
        if running:
            devices = st.get("devices") or []
            state = st.get("state") or "idle"
            if devices:
                n = len(devices)
                sub = f"{state} · listening on {n} keyboard{'s' if n != 1 else ''}"
                self.status_dot.add_css_class("on")
            else:
                sub = f"{state} · can't read your keyboard yet, see setup below"
                self.status_dot.add_css_class("warn")
            if not st.get("layer_shell", True):
                sub += " · basic pill"
            self.status_row.set_title("kivi is running")
            self.status_row.set_subtitle(sub)
        elif active == "active":
            self.status_row.set_title("kivi is starting…")
            self.status_row.set_subtitle("the service is up but not answering yet")
            self.status_dot.add_css_class("warn")
        else:
            self.status_row.set_title("kivi isn't running")
            self.status_row.set_subtitle("start it to use your shortcuts and the pill"
                                         if enabled != "not-found" and enabled
                                         else "service not installed: run install.sh, or start it here")
        self.btn_start.set_visible(not running and active != "active")
        self.btn_restart.set_visible(running or active == "active")
        self.btn_stop.set_visible(running or active == "active")
        self._updating = True
        self.login_row.set_active(enabled == "enabled")
        self.login_row.set_sensitive(bool(enabled) and enabled != "not-found")
        self._updating = False

    def _daemon(self, verb):
        self.status_spin.set_visible(True)
        for b in (self.btn_start, self.btn_restart, self.btn_stop):
            b.set_sensitive(False)

        def work():
            rc_cat, _ = _systemctl("cat", "kivi")
            if rc_cat == 0:
                rc, out = _systemctl(verb, "kivi", timeout=15)
                if rc != 0:
                    raise RuntimeError(out or f"systemctl {verb} failed")
            else:  # no unit installed: talk to / spawn the daemon directly
                if verb in ("stop", "restart"):
                    _ipc({"cmd": "quit"})
                    time.sleep(0.6)
                if verb in ("start", "restart"):
                    exe = _kivi_exe()
                    if not exe:
                        raise RuntimeError("couldn't find the kivi command; run install.sh")
                    subprocess.Popen([exe, "daemon"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(1.0)

        def done(_res, err):
            self.status_spin.set_visible(False)
            for b in (self.btn_start, self.btn_restart, self.btn_stop):
                b.set_sensitive(True)
            if err:
                self.toast(str(err))
            self._refresh_status()

        run_bg(work, done)

    def _set_login(self, on):
        if self._updating:
            return

        def done(res, err):
            rc, out = res or (1, str(err))
            if rc != 0:
                self.toast(out or "couldn't change launch at login")
            self._refresh_status()

        run_bg(lambda: _systemctl("enable" if on else "disable", "kivi"), done)

    def _probe_checks(self):
        key = bool(os.environ.get("SARVAM_API_KEY") or _safe(getattr(config, "get_api_key", None)))
        d = desktop
        return {
            "key": key,
            "input": _safe(getattr(d, "input_access_ok", None)),
            "uinput": _safe(getattr(d, "uinput_ok", None)),
            "layer": _safe(getattr(d, "layer_shell_available", None)),
            "wl": bool(shutil.which("wl-copy") and shutil.which("wl-paste")),
        }

    def _render_checks(self, res, err):
        res = res or {}
        self.check_list.remove_all()

        def add(ok, title, ok_sub, bad_sub, fixes=(), optional=False, action=None):
            if ok or not fixes:
                row = Adw.ActionRow(title=title, subtitle=ok_sub if ok else bad_sub)
            else:
                row = Adw.ExpanderRow(title=title, subtitle=bad_sub)
                for cmd, note in fixes:
                    inner = Adw.ActionRow(title=cmd, subtitle=note or "", title_selectable=True, use_markup=False)
                    inner.add_css_class("kivi-mono")
                    inner.add_suffix(button("", "flat", icon="edit-copy-symbolic", tooltip="copy command",
                                            on_click=lambda c=cmd: (copy_to_clipboard(c), self.toast("copied"))))
                    row.add_row(inner)
            if action and not ok:
                row.add_suffix(action)
            mark = "✓" if ok else ("–" if optional else "✕")
            badge = label(mark, "kivi-check", "ok" if ok else ("optional" if optional else "bad"),
                          xalign=0.5, valign=Gtk.Align.CENTER)
            row.add_prefix(badge)
            self.check_list.append(row)

        if res.get("input") is None and desktop is None:
            res["input"] = res["uinput"] = res["layer"] = False
        add(res.get("key"), "sarvam api key", "saved", "kivi needs a key to hear you",
            action=button("add key", "kivi-ghost", "small", on_click=lambda: self.show_page("api-key")))
        add(res.get("input"), "keyboard access", "kivi can see your shortcuts",
            "add yourself to the input group so kivi can see your shortcut",
            [(FIX_INPUT, "then log out and back in"), (FIX_INPUT_NOW, "works right away, until reboot")])
        add(res.get("uinput"), "typing access", "kivi can type into any app",
            "kivi types through a virtual keyboard (/dev/uinput)", [(FIX_UINPUT, "until reboot")])
        add(res.get("layer"), "layer-shell (optional)", "the pill floats above everything",
            "nicer always-on-top pill on wayland", [(FIX_LAYER_SHELL, "then restart kivi")], optional=True)
        add(res.get("wl"), "wl-clipboard (optional)", "fast clipboard on wayland",
            "kivi falls back to klipper without it", [(FIX_WL_CLIPBOARD, "")], optional=True)

    # ============================================================ 2. api key
    def _page_api_key(self):
        scroller, box = self._scaffold("api key", "kivi runs on sarvam ai. paste your api subscription key; "
                                                  "it stays on this computer, readable only by you.")
        g = group("sarvam ai")
        self.key_row = Adw.PasswordEntryRow(title="api subscription key")
        self.key_row.set_text(_safe(getattr(config, "get_api_key", None), default="") or "")
        self.key_row.connect("entry-activated", lambda *_: self._save_key())
        g.add(self.key_row)
        box.append(g)

        actions = hbox(10)
        actions.append(button("save", "suggested-action", on_click=self._save_key))
        self.test_btn = button("test key", "kivi-ghost", on_click=self._test_key)
        actions.append(self.test_btn)
        self.key_spin = Adw.Spinner(visible=False, valign=Gtk.Align.CENTER)
        actions.append(self.key_spin)
        self.key_result = label("", "kivi-ink-2", wrap=True, hexpand=True)
        actions.append(self.key_result)
        box.append(actions)

        info = vbox(10, "kivi-card")
        info.append(label("no key yet?", "kivi-display-sm"))
        info.append(label("sign in at the sarvam dashboard, create an api key and paste it above. "
                          "kivi uses saaras for speech, sarvam-105b for polishing and bulbul for its voice.",
                          "kivi-ink-2", wrap=True))
        link = Gtk.LinkButton(uri="https://dashboard.sarvam.ai", label="open dashboard.sarvam.ai",
                              halign=Gtk.Align.START)
        info.append(link)
        if os.environ.get("SARVAM_API_KEY"):
            info.append(label("note: SARVAM_API_KEY is set in this environment and takes priority over "
                              "the saved key.", "kivi-warning", wrap=True))
        box.append(info)
        return scroller

    def _save_key(self):
        key = self.key_row.get_text().strip()
        if not key:
            self.key_result.set_text("paste a key first.")
            return
        if not hasattr(config, "set_api_key"):
            self.toast("couldn't save: config module unavailable")
            return
        try:
            config.set_api_key(key)
        except Exception as e:
            self.toast(f"couldn't save the key: {e}")
            return
        self.key_result.set_text("saved.")
        self.toast("key saved")
        self.reload_daemon()

    def _test_key(self):
        key = self.key_row.get_text().strip()
        if not key:
            self.key_result.set_text("paste a key first.")
            return
        if not hasattr(sarvam, "check_key"):
            self.key_result.set_text("can't test: sarvam module unavailable.")
            return
        self.key_spin.set_visible(True)
        self.test_btn.set_sensitive(False)
        self.key_result.set_text("checking with sarvam…")

        def done(res, err):
            self.key_spin.set_visible(False)
            self.test_btn.set_sensitive(True)
            ok, msg = res if res else (False, str(err))
            for c in ("kivi-green", "kivi-error"):
                self.key_result.remove_css_class(c)
            self.key_result.add_css_class("kivi-green" if ok else "kivi-error")
            self.key_result.set_text(("✓ " if ok else "✕ ") + (msg or ("key works." if ok else "key rejected.")))

        run_bg(lambda: sarvam.check_key(key), done)

    # ============================================================ 3. shortcuts
    def _page_shortcuts(self):
        scroller, box = self._scaffold("shortcuts", "hold to talk. let go, kivi types. quick taps still "
                                                    "work like they always did.")
        self.input_banner = Adw.Banner(title="kivi can't read your keyboard yet", button_label="fix it",
                                       revealed=False, visible=False)
        self.input_banner.connect("button-clicked", lambda *_: self.show_page("home"))
        box.append(self.input_banner)

        g = group("hold to")
        self.sc_caps = {}
        for action, title, sub in SHORTCUT_ROWS:
            row = Adw.ActionRow(title=title, subtitle=sub, use_markup=False)
            holder = hbox(0, valign=Gtk.Align.CENTER)
            self.sc_caps[action] = holder
            row.add_suffix(holder)
            row.add_suffix(button("change", "kivi-ghost", "small", on_click=lambda a=action, t=title: self._capture(a, t)))
            reset = button("", "flat", icon="edit-undo-symbolic", tooltip="reset to default",
                           on_click=lambda a=action: self._set_shortcut(a, self.defaults["shortcuts"][a]))
            row.add_suffix(reset)
            g.add(row)
        box.append(g)
        self._render_shortcuts()

        b = group("behaviour")
        b.add(switch_row("hands-free", "double-tap dictate to keep listening. tap once more to finish.",
                         self.get("hands_free"), lambda v: self.set("hands_free", v)))
        hold = Adw.ActionRow(title="hold time", subtitle="how long to hold before kivi starts listening")
        value = label("", "kivi-mono", "kivi-ink-2", width_chars=5, xalign=1.0)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.15, 1.0, 0.05)
        scale.set_draw_value(False)
        scale.set_size_request(180, -1)
        scale.set_valign(Gtk.Align.CENTER)
        scale.set_value(float(self.get("min_hold") or 0.35))

        def on_hold(s):
            v = round(s.get_value() / 0.05) * 0.05
            value.set_text(f"{v:.2f} s")
            self.set("min_hold", round(v, 2))

        scale.connect("value-changed", on_hold)
        value.set_text(f"{scale.get_value():.2f} s")
        hold.add_suffix(scale)
        hold.add_suffix(value)
        b.add(hold)
        b.add(combo_row("paste with", "use ctrl+shift+v if you mostly dictate into terminals",
                        [("ctrl+v", "ctrl + v"), ("ctrl+shift+v", "ctrl + shift + v"), ("shift+insert", "shift + insert")],
                        self.get("paste_keys"), lambda v: self.set("paste_keys", v)))
        b.add(combo_row("copy with", "how command mode reads your selection; terminals need ctrl+shift+c",
                        [("ctrl+c", "ctrl + c"), ("ctrl+shift+c", "ctrl + shift + c"), ("ctrl+insert", "ctrl + insert")],
                        self.get("copy_keys") or "ctrl+c", lambda v: self.set("copy_keys", v)))
        b.add(switch_row("restore clipboard", "put back what you had copied after kivi types",
                         self.get("restore_clipboard"), lambda v: self.set("restore_clipboard", v)))
        box.append(b)

        foot = label("no keyboard access? bind <tt>kivi toggle dictate</tt> or <tt>kivi toggle agent</tt> to a "
                     "shortcut in system settings instead.", "kivi-dim", wrap=True, markup=True)
        box.append(foot)

        def refresh():
            self._render_shortcuts()
            run_bg(lambda: _safe(getattr(desktop, "input_access_ok", None), default=True),
                   lambda ok, _e: (self.input_banner.set_visible(ok is False),
                                   self.input_banner.set_revealed(ok is False)))

        self._refreshers["shortcuts"] = refresh
        if os.environ.get("KIVI_SETTINGS_DIALOG") == "capture":
            GLib.timeout_add(600, lambda: (self._capture("dictate", "dictate"), False)[1])
        return scroller

    def _render_shortcuts(self):
        for action, holder in self.sc_caps.items():
            while (c := holder.get_first_child()):
                holder.remove(c)
            holder.append(keycaps(self.get(f"shortcuts.{action}") or ""))

    def _capture(self, action, title):
        names = {a: t for a, t, _ in SHORTCUT_ROWS}
        taken = {self.get(f"shortcuts.{a}"): names[a] for a in names if a != action}
        dialog = CaptureDialog(title, lambda combo: self._set_shortcut(action, combo), taken)
        # keep the running daemon's hotkeys quiet while the user presses keys here
        run_bg(lambda: _ipc({"cmd": "capture", "active": True}, timeout=1.0))
        dialog.connect("closed", lambda *_: run_bg(lambda: _ipc({"cmd": "capture", "active": False}, timeout=1.0)))
        dialog.present(self)

    def _set_shortcut(self, action, combo):
        self.set(f"shortcuts.{action}", combo)
        self._render_shortcuts()
        self.toast(f"{dict((a, t) for a, t, _ in SHORTCUT_ROWS)[action]}: {combo_label(combo)}")

    # ============================================================ 4. voice & language
    def _page_voice(self):
        scroller, box = self._scaffold("voice & language", "speak hindi, english, or both in the same breath. "
                                                           "kivi keeps your language and script.")
        d = group("dictation")
        d.add(combo_row("language", "auto-detect handles hinglish and switching mid-sentence",
                        LANGUAGES, self.get("language"), lambda v: self.set("language", v)))
        d.add(switch_row("polish my words", "remove fillers, fix punctuation, apply “no wait” corrections",
                         self.get("cleanup"), lambda v: self.set("cleanup", v)))
        box.append(d)

        a = group("kivi's voice", "how the voice agent sounds when it answers you.")
        a.add(switch_row("speak replies", "read agent answers out loud", self.get("agent_speak"),
                         lambda v: self.set("agent_speak", v)))
        a.add(combo_row("voice", "bulbul v3 by sarvam", [(s, s) for s in TTS_SPEAKERS],
                        self.get("tts_speaker"), lambda v: self.set("tts_speaker", v)))
        a.add(combo_row("speaking language", "", TTS_LANGUAGES, self.get("tts_language"),
                        lambda v: self.set("tts_language", v)))
        prev = Adw.ActionRow(title="preview voice", subtitle="hear the selected voice")
        self.preview_spin = Adw.Spinner(visible=False, valign=Gtk.Align.CENTER)
        self.preview_btn = button("play", "kivi-ghost", icon="media-playback-start-symbolic",
                                  on_click=self._preview_voice)
        prev.add_suffix(self.preview_spin)
        prev.add_suffix(self.preview_btn)
        a.add(prev)
        box.append(a)
        return scroller

    def _preview_voice(self):
        key = _safe(getattr(config, "get_api_key", None), default="") or os.environ.get("SARVAM_API_KEY", "")
        if not key:
            self.toast("add your sarvam api key first")
            return
        if not hasattr(sarvam, "tts"):
            self.toast("can't preview: sarvam module unavailable")
            return
        lang, speaker = self.get("tts_language") or "en-IN", self.get("tts_speaker") or "shubh"
        text = PREVIEW_TEXT.get(lang, PREVIEW_TEXT["en-IN"])
        self.preview_spin.set_visible(True)
        self.preview_btn.set_sensitive(False)

        def work():
            data = sarvam.tts(key, text, language=lang, speaker=speaker)
            if audio and hasattr(audio, "play_wav_bytes"):
                audio.play_wav_bytes(data)
                return
            fd, path = tempfile.mkstemp(prefix="kivi-preview-", suffix=".wav")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            player = shutil.which("pw-play") or shutil.which("aplay")
            if not player:
                raise RuntimeError("no audio player (pw-play) found")
            try:
                subprocess.run([player, path], timeout=60)
            finally:
                os.unlink(path)

        def done(_res, err):
            self.preview_spin.set_visible(False)
            self.preview_btn.set_sensitive(True)
            if err:
                self.toast(f"preview failed: {err}")

        run_bg(work, done)

    # ============================================================ 5. style
    def _page_style(self):
        scroller, box = self._scaffold("style", "choose how you sound. write kivi your preferences in plain "
                                                "words and it follows them, exactly.")
        frame, self.style_view = textarea("e.g. casual, all lowercase, no full stops. keep my hinglish.", 150)
        buf = self.style_view.get_buffer()
        buf.set_text(self.get("style") or "")
        buf.connect("changed", lambda b: self.set("style", b.get_text(b.get_start_iter(), b.get_end_iter(),
                                                                       False).strip()))
        box.append(frame)

        ex = vbox(12)
        ex.append(label("START FROM A PERSONA", "kivi-eyebrow"))
        chips = Adw.WrapBox(child_spacing=8, line_spacing=8)
        self.persona_quote = label("", "kivi-quote", wrap=True)
        for name, text in PERSONAS:
            chip = button(name, "kivi-chip", tooltip=text, on_click=lambda t=text: buf.set_text(t))
            hover = Gtk.EventControllerMotion()
            hover.connect("enter", lambda *_a, t=text, n=name: self.persona_quote.set_text(f"“{t}”  · {n}"))
            chip.add_controller(hover)
            chips.append(chip)
        chips.append(button("clear", "kivi-chip", on_click=lambda: buf.set_text("")))
        ex.append(chips)
        box.append(ex)

        card = vbox(10, "kivi-card")
        card.append(label("HOW IT WORKS", "kivi-eyebrow"))
        n, t = PERSONAS[0]
        self.persona_quote.set_text(f"“{t}”  · {n}")
        card.append(self.persona_quote)
        card.append(label("your style is applied every time kivi polishes a dictation. it never changes what "
                          "you meant, only how it reads.", "kivi-ink-2", wrap=True))
        box.append(card)
        return scroller

    # ============================================================ 6. memory
    def _page_memory(self):
        scroller, box = self._scaffold("memory", "names, brands and jargon kivi keeps getting wrong. "
                                                 "teach it once, it spells them right from then on.")
        g = group()
        self.word_entry = Adw.EntryRow(title="add a word or name", show_apply_button=True)
        self.word_entry.connect("apply", lambda *_: self._add_word())
        self.word_entry.connect("entry-activated", lambda *_: self._add_word())
        g.add(self.word_entry)
        box.append(g)

        card = vbox(14, "kivi-card")
        head = hbox(8)
        head.append(label("kivi remembered", "kivi-display-sm", hexpand=True))
        self.word_count = label("", "kivi-dim")
        head.append(self.word_count)
        card.append(head)
        self.words_box = Adw.WrapBox(child_spacing=8, line_spacing=8)
        card.append(self.words_box)
        self.words_empty = label("nothing yet. add a name above, or say “hey kivi, remember Siddharth”.",
                                 "kivi-dim", wrap=True)
        card.append(self.words_empty)
        box.append(card)
        self._refreshers["memory"] = self._render_words
        self._render_words()
        return scroller

    def _render_words(self):
        words = _safe(getattr(config, "load_words", None), default=[]) or []
        self.words_box.remove_all()
        for w in words:
            chip = hbox(4, "kivi-word")
            chip.append(label(w, valign=Gtk.Align.CENTER))
            chip.append(button("", "flat", icon="window-close-symbolic", tooltip="forget",
                               on_click=lambda w=w: self._remove_word(w)))
            self.words_box.append(chip)
        self.words_empty.set_visible(not words)
        self.word_count.set_text(f"{len(words)} word{'s' if len(words) != 1 else ''}" if words else "")

    def _add_word(self):
        word = self.word_entry.get_text().strip()
        if not word:
            return
        if _safe(getattr(config, "add_word", None), word, default=False) is False:
            self.toast("couldn't save the word")
            return
        self.word_entry.set_text("")
        self._render_words()
        self.toast(f"kivi remembered {word}")
        self.reload_daemon()

    def _remove_word(self, word):
        if _safe(getattr(config, "remove_word", None), word, default=False) is False:
            self.toast("couldn't remove the word")
            return
        self._render_words()
        self.toast(f"forgot {word}")
        self.reload_daemon()

    # ============================================================ 7. history
    def _page_history(self):
        scroller, box = self._scaffold("history", "everything you said, and what kivi did with it. "
                                                  "kept only on this computer.")
        g = group()
        g.add(switch_row("keep history", "save the last 500 runs locally", self.get("history"),
                         lambda v: self.set("history", v)))
        box.append(g)

        bar = hbox(8)
        self.hist_count = label("", "kivi-dim", hexpand=True, valign=Gtk.Align.CENTER)
        bar.append(self.hist_count)
        bar.append(button("refresh", "flat", icon="view-refresh-symbolic", tooltip="refresh",
                          on_click=lambda: self._render_history()))
        self.hist_clear = button("clear all", "destructive-action", on_click=self._confirm_clear)
        bar.append(self.hist_clear)
        box.append(bar)

        self.hist_list = vbox(12)
        box.append(self.hist_list)
        self._refreshers["history"] = self._render_history
        return scroller

    def _render_history(self):
        while (c := self.hist_list.get_first_child()):
            self.hist_list.remove(c)
        entries = _safe(getattr(config, "load_history", None), 200, default=[]) or []
        self.hist_count.set_text(f"{len(entries)} run{'s' if len(entries) != 1 else ''}" if entries else "")
        self.hist_clear.set_sensitive(bool(entries))
        if not entries:
            empty = vbox(10, "kivi-card", margin_top=8)
            empty.append(bars(dim=True, scale=1.6))
            empty.append(label("nothing here yet.", "kivi-display-sm", xalign=0.5))
            empty.append(label(f"hold {combo_label(self.get('shortcuts.dictate') or '')} and say something.",
                               "kivi-dim", xalign=0.5))
            self.hist_list.append(empty)
            return
        for e in entries:
            if isinstance(e, dict):
                self.hist_list.append(self._history_card(e))

    def _history_card(self, e):
        mode = e.get("mode") or "dictate"
        card = vbox(8, "kivi-card", "tight")
        top = hbox(8)
        top.append(label(MODE_TAGS.get(mode, mode), "kivi-tag", mode, valign=Gtk.Align.CENTER))
        meta = [time_ago(e.get("ts"))]
        if e.get("language"):
            meta.append(str(e["language"]))
        if e.get("ms"):
            meta.append(f"{int(e['ms'])} ms")
        top.append(label(" · ".join(m for m in meta if m), "kivi-dim", hexpand=True, valign=Gtk.Align.CENTER))
        result = str(e.get("result") or "")
        top.append(button("", "flat", icon="edit-copy-symbolic", tooltip="copy result",
                          on_click=lambda: (copy_to_clipboard(result), self.toast("copied"))))
        card.append(top)
        if e.get("heard"):
            said = vbox(2)
            said.append(label("YOU SAID", "kivi-said-label"))
            said.append(label(str(e["heard"]), "kivi-said", wrap=True, selectable=True))
            card.append(said)
        if result:
            card.append(label(result, "kivi-result", wrap=True, selectable=True))
        return card

    def _confirm_clear(self):
        dlg = Adw.AlertDialog.new("clear all history?", "this deletes every saved run. it can't be undone.")
        dlg.add_response("cancel", "keep it")
        dlg.add_response("clear", "clear all")
        dlg.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def on_response(_d, resp):
            if resp == "clear":
                _safe(getattr(config, "clear_history", None))
                self._render_history()
                self.toast("history cleared")

        dlg.connect("response", on_response)
        dlg.present(self)

    # ============================================================ 8. agent
    def _page_agent(self):
        agent_keys = combo_label(self.get("shortcuts.agent") or "")
        scroller, box = self._scaffold("agent", f"hold {agent_keys} (or click the pill) and ask. kivi lives on "
                                                "your desktop and does things for you, then answers in a line "
                                                "or two.")
        card = vbox(12, "kivi-card")
        card.append(label("TRY SAYING", "kivi-eyebrow"))
        for q in ("“hey kivi, open youtube and search lo-fi beats”",
                  "“type a polite reply saying i'll be ten minutes late”",
                  "“what's this error mean?” with text selected"):
            card.append(label(q, "kivi-quote", wrap=True))
        can = Adw.WrapBox(child_spacing=6, line_spacing=6, margin_top=6)
        for c in ("types for you", "opens sites", "launches apps", "copies things", "reads your selection",
                  "remembers words"):
            can.append(label(c, "kivi-chip", "green"))
        card.append(can)
        box.append(card)

        p = group("permissions")
        self.shell_row = switch_row("allow shell commands",
                                    "kivi may run commands you ask for with bash (15 s limit). only turn this "
                                    "on if you're comfortable with that.",
                                    self.get("agent_allow_shell"), self._set_shell)
        warn = Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.CENTER)
        warn.add_css_class("kivi-amber")
        self.shell_row.add_prefix(warn)
        p.add(self.shell_row)
        box.append(p)

        pill = group("the pill", "kivi's little always-on home at the edge of your screen.")
        pill.add(switch_row("show the pill", "", self.get("overlay.enabled"),
                            lambda v: self.set("overlay.enabled", v)))
        pill.add(combo_row("position", "", [("bottom", "bottom of the screen"), ("top", "top of the screen")],
                           self.get("overlay.position"), lambda v: self.set("overlay.position", v)))
        pill.add(combo_row("when idle", "", [("pill", "small pill, always there"), ("hidden", "hidden until you talk")],
                           self.get("overlay.idle"), lambda v: self.set("overlay.idle", v)))
        margin = Adw.SpinRow.new_with_range(0, 200, 2)
        margin.set_title("distance from edge")
        margin.set_subtitle("pixels")
        saved_margin = self.get("overlay.margin")
        margin.set_value(float(saved_margin if saved_margin is not None else 28))
        margin.connect("notify::value", lambda r, _p: self.set("overlay.margin", int(r.get_value())))
        pill.add(margin)
        pill.add(switch_row("notifications", "when the pill is turned off, show progress as desktop notifications",
                            self.get("notify"), lambda v: self.set("notify", v)))
        box.append(pill)
        return scroller

    def _set_shell(self, on):
        if self._updating:
            return
        if not on:
            self.set("agent_allow_shell", False)
            return
        dlg = Adw.AlertDialog.new("let kivi run commands?",
                                  "the agent will be able to run shell commands on your behalf when you ask. "
                                  "speech can be misheard, so keep an eye on it.")
        dlg.add_response("cancel", "not now")
        dlg.add_response("allow", "allow")
        dlg.set_response_appearance("allow", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def on_response(_d, resp):
            if resp == "allow":
                self.set("agent_allow_shell", True)
            else:
                self._updating = True
                self.shell_row.set_active(False)
                self._updating = False

        dlg.connect("response", on_response)
        dlg.present(self)


# ---------------------------------------------------------------- app

class SettingsApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        GLib.set_application_name("kivi")
        theme_icons = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        Gtk.Window.set_default_icon_name("kivi" if theme_icons.has_icon("kivi") else "audio-input-microphone")
        _safe(apply_css)

    def do_activate(self):
        if not self.window:
            self.window = SettingsWindow(self)
        self.window.present()


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    return SettingsApp().run(argv[:1])


if __name__ == "__main__":
    sys.exit(main())
