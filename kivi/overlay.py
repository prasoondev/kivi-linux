"""The always-on kivi pill: a small dark capsule at the bottom of the screen that wakes up,
listens (five green bars riding the mic level), thinks, and shows what it heard or answered.

Placement backends, best first:
  layer-shell  gtk4-layer-shell (needs the lib in LD_PRELOAD before Gtk loads; daemon.py does that)
  kwin         KDE without layer-shell: a KWin script pins/positions the window and hands focus back
  plain        anything else: an undecorated window (callers should also notify)

Run `python3 -m kivi.overlay --demo` to cycle through every state.
"""

import math
import os
import sys
import tempfile
import time
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")
import cairo  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from . import theme  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
except (ValueError, ImportError):
    LayerShell = None

CAPTION = "kivi-overlay"
KWIN_PLUGIN = "kivi-overlay"
LAYER_LIB = "libgtk4-layer-shell"

WIN_W, WIN_H = 520, 360       # fixed transparent canvas; input region limits clicks to what's drawn
CARD_GAP = 10                  # space between the pill and a card above (or below) it
EDGE_PAD = 16                  # gap between pill edge and window edge on the screen side
PILL_MAX_W = 440
CARD_MAX_W = 420

FONT_BODY = theme.FONT_BODY
FONT_MONO = f"{theme.FONT_MONO}, JetBrains Mono, Noto Sans Mono, monospace"

MODE_LABELS = {"dictate": "listening", "command": "editing", "agent": "kivi agent"}
MENU_ITEMS = (("settings", "settings"), ("pause", "pause kivi"), ("hide", "hide pill"), ("quit", "quit"))


def _bezier(x1, y1, x2, y2):
    """CSS cubic-bezier easing function."""
    def coord(t, a, b):
        return ((1 - 3 * b + 3 * a) * t + (3 * b - 6 * a)) * t * t + 3 * a * t

    def solve(x):
        t = x
        for _ in range(8):
            dx = coord(t, x1, x2) - x
            d = (3 * (1 - 3 * x2 + 3 * x1) * t + 2 * (3 * x2 - 6 * x1)) * t + 3 * x1
            if abs(dx) < 1e-5 or abs(d) < 1e-6:
                break
            t -= dx / d
        return coord(min(1.0, max(0.0, t)), y1, y2)

    return lambda x: 0.0 if x <= 0 else 1.0 if x >= 1 else solve(x)


EASE = _bezier(*theme.EASE_ENTER)


class Tween:
    """A value that eases toward its target over `ms` milliseconds."""

    def __init__(self, value: float, ms: float = 320):
        self.value = self.target = self._from = value
        self.ms = ms
        self._t0 = 0.0

    def to(self, target: float, now: float, ms: float | None = None):
        if target == self.target:
            return
        self._from, self.target, self._t0 = self.value, target, now
        if ms is not None:
            self.ms = ms

    def snap(self, value: float):
        self.value = self.target = self._from = value

    def step(self, now: float) -> bool:
        """Advance; returns True while still moving."""
        if self.value == self.target:
            return False
        p = (now - self._t0) * 1000 / max(1.0, self.ms)
        if p >= 1:
            self.value = self.target
            return False
        self.value = self._from + (self.target - self._from) * EASE(p)
        return True


def _rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def _combo_label(combo: str) -> str:
    try:
        from .hotkeys import combo_label
        return combo_label(combo)
    except Exception:
        parts = []
        for k in combo.split("+"):
            k = k.strip().upper().removeprefix("KEY_")
            for side in ("RIGHT", "LEFT"):
                if k.startswith(side) and len(k) > len(side):
                    k = f"{side} {k[len(side):]}"
            parts.append(k.lower().replace("meta", "super"))
        return " + ".join(parts)


def _clip_text(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------------------------- KWin

_KWIN_JS = r"""
(function () {
    var CAPTION = %(caption)s, POS = %(pos)s, MARGIN = %(margin)d;
    var me = null, prev = null;

    function isMe(w) { return !!w && typeof w.caption === "string" && w.caption.indexOf(CAPTION) === 0; }
    function targetOutput() {
        var a = workspace.activeWindow;
        if (a && !isMe(a) && a.output) return a.output;
        return workspace.activeScreen;
    }
    function place() {
        if (!me) return;
        var area;
        try { area = workspace.clientArea(KWin.MaximizeArea, targetOutput(), workspace.currentDesktop); }
        catch (e) { area = workspace.clientArea(KWin.PlacementArea, me); }
        var g = me.frameGeometry;
        var x = Math.round(area.x + (area.width - g.width) / 2);
        var y = Math.round(POS === "top" ? area.y + MARGIN : area.y + area.height - g.height - MARGIN);
        if (g.x !== x || g.y !== y) me.frameGeometry = { x: x, y: y, width: g.width, height: g.height };
    }
    function giveBackFocus() {
        if (prev && prev !== me) { try { workspace.activeWindow = prev; } catch (e) {} }
    }
    function adopt(w) {
        if (!isMe(w)) return;
        me = w;
        w.keepAbove = true;
        w.skipTaskbar = true;
        w.skipPager = true;
        w.skipSwitcher = true;
        w.onAllDesktops = true;
        w.noBorder = true;
        try { w.skipsCloseAnimation = true; } catch (e) {}
        place();
        w.frameGeometryChanged.connect(place);
        if (workspace.activeWindow === w) giveBackFocus();
    }

    var a = workspace.activeWindow;
    if (a && !isMe(a)) prev = a;
    workspace.windowList().forEach(adopt);
    workspace.windowAdded.connect(adopt);
    workspace.windowRemoved.connect(function (w) {
        if (w === me) me = null;
        if (w === prev) prev = null;
    });
    // the pill must never keep keyboard focus: pasting goes to whatever the user was in
    workspace.windowActivated.connect(function (w) {
        if (!w) return;
        if (isMe(w)) { giveBackFocus(); return; }
        if (w.normalWindow || w.dialog) prev = w;
        place();
    });
    workspace.screensChanged.connect(place);
    try { workspace.virtualScreenGeometryChanged.connect(place); } catch (e) {}
    try { workspace.currentDesktopChanged.connect(place); } catch (e) {}
})();
"""


class KWinPlacer:
    """Loads (and replaces) a KWin script that keeps the overlay window pinned and unfocused."""

    def __init__(self):
        self._bus = None
        self._path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
                                  f"kivi-overlay-{os.getuid()}.js")

    @staticmethod
    def available() -> bool:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            res = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "NameHasOwner", GLib.Variant("(s)", ("org.kde.KWin",)),
                                GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
            return bool(res.unpack()[0])
        except GLib.Error:
            return False

    def _call(self, path, iface, method, args=None, reply=None):
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = self._bus.call_sync("org.kde.KWin", path, iface, method, args,
                                  GLib.VariantType(reply) if reply else None,
                                  Gio.DBusCallFlags.NONE, 3000, None)
        return res.unpack()[0] if reply else None

    def load(self, position: str, margin: int) -> bool:
        js = _KWIN_JS % {"caption": f'"{CAPTION}"', "pos": '"top"' if position == "top" else '"bottom"',
                         "margin": int(margin)}
        try:
            with open(self._path, "w") as f:
                f.write(js)
            self.unload()
            sid = self._call("/Scripting", "org.kde.kwin.Scripting", "loadScript",
                             GLib.Variant("(ss)", (self._path, KWIN_PLUGIN)), "(i)")
            if sid < 0:
                return False
            try:
                self._call(f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run")
            except GLib.Error:
                self._call("/Scripting", "org.kde.kwin.Scripting", "start")
            return True
        except (GLib.Error, OSError) as e:
            print(f"kivi: kwin script failed: {e}", file=sys.stderr)
            return False

    def unload(self):
        try:
            if self._call("/Scripting", "org.kde.kwin.Scripting", "isScriptLoaded",
                          GLib.Variant("(s)", (KWIN_PLUGIN,)), "(b)"):
                self._call("/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                           GLib.Variant("(s)", (KWIN_PLUGIN,)), "(b)")
        except GLib.Error:
            pass


def _pick_backend() -> str:
    forced = os.environ.get("KIVI_OVERLAY_BACKEND", "").strip().lower()
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    layer_ok = (LayerShell is not None and wayland and LAYER_LIB in os.environ.get("LD_PRELOAD", "")
                and LayerShell.is_supported())
    if forced in ("layer-shell", "kwin", "plain"):
        if forced != "layer-shell" or layer_ok:
            return forced
    if layer_ok:
        return "layer-shell"
    if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() and KWinPlacer.available():
        return "kwin"
    return "plain"


# ------------------------------------------------------------------------------------- overlay

class Overlay:
    def __init__(self, app: Gtk.Application, cfg, on_click: Callable[[], None], on_menu: Callable[[str], None]):
        self.app = app
        self.on_click = on_click
        self.on_menu = on_menu
        self.backend = _pick_backend()
        self.available = self.backend in ("layer-shell", "kwin")  # False => caller should also notify
        self.paused = False
        self.hint = ""             # e.g. "no keyboard access" set by the daemon
        self._user_hidden = False
        self._kwin = KWinPlacer() if self.backend == "kwin" else None

        self.state = "idle"
        self._mode = "dictate"
        self._text = ""
        self._t_listen = 0.0
        self._hover = False
        self._timers: dict[str, int] = {}
        self._pending_idle = False
        self._level = 0.0
        self._level_target = 0.0
        self._level_at = 0.0
        self._bars = list(theme.BAR_REST)
        self._phases = [0.0, 1.7, 3.1, 0.9, 2.4]
        self._freqs = [5.3, 7.1, 6.2, 8.3, 5.9]
        self._last_t = 0.0
        self._tick_id = None
        self._region = None
        self._layouts: dict[str, Pango.Layout] = {}
        self._card = None
        self._items = []

        self._w, self._h = Tween(46, 380), Tween(16, 380)
        self._alpha = Tween(1.0, 220)
        self._content = Tween(1.0, 260)
        self._glow = Tween(0.0, 400)
        self._card_a = Tween(0.0, 260)

        self._load_cfg(cfg)
        theme.apply(theme.OVERLAY_CSS)
        self._build()
        self._set_state("idle", {})
        self._snap()
        if self._kwin:
            self._kwin.load(self._position, self._edge_margin())
            app.connect("shutdown", lambda *_: self._kwin.unload())
        self.win.set_visible(True)   # not present(): never ask the compositor for activation

    # -- public API ---------------------------------------------------------------------------

    def handle(self, event: str, data: dict):
        """Thread-safe entry point for engine UI events."""
        GLib.idle_add(self._handle, event, dict(data or {}))

    def reload(self, cfg):
        GLib.idle_add(self._reload, cfg)

    def set_visible(self, visible: bool):
        """Show/hide the idle pill (IPC "overlay" / menu "hide pill"); active states always show."""
        GLib.idle_add(self._set_user_hidden, not visible)

    def set_paused(self, paused: bool):
        GLib.idle_add(self._set_paused, paused)

    def set_hint(self, text: str):
        """Replace the hover hint, e.g. "no keyboard access — click to talk"."""
        GLib.idle_add(self._set_hint, text or "")

    # -- config -------------------------------------------------------------------------------

    def _load_cfg(self, cfg):
        o = (cfg or {}).get("overlay", {}) or {}
        self._enabled = bool(o.get("enabled", True))
        self._position = "top" if o.get("position") == "top" else "bottom"
        self._idle_style = o.get("idle", "pill")
        self._margin = int(o.get("margin", 28))
        sc = (cfg or {}).get("shortcuts", {}) or {}
        self._dictate_label = _combo_label(sc.get("dictate", "RIGHTCTRL"))

    def _edge_margin(self) -> int:
        return max(0, self._margin - EDGE_PAD)

    def _reload(self, cfg):
        old = (self._position, self._margin)
        self._load_cfg(cfg)
        self._user_hidden = False   # a reload makes the config authoritative again
        if (self._position, self._margin) != old:
            self._apply_position()
        self._set_state(self.state, {}, refresh=True)
        return False

    def _set_user_hidden(self, hidden):
        self._user_hidden = hidden
        self._set_state(self.state, {}, refresh=True)
        return False

    def _set_paused(self, paused):
        self.paused = paused
        self._menu_buttons["pause"].set_label("resume kivi" if paused else "pause kivi")
        self._set_state(self.state, {}, refresh=True)
        return False

    def _set_hint(self, text):
        self.hint = text
        self.area.set_tooltip_text(text or None)
        self._set_state(self.state, {}, refresh=True)
        return False

    # -- widgets ------------------------------------------------------------------------------

    def _build(self):
        win = self.win = Gtk.Window(application=self.app, title=CAPTION)
        win.add_css_class("kivi-overlay")
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_default_size(WIN_W, WIN_H)
        win.set_size_request(WIN_W, WIN_H)
        win.set_focusable(False)
        win.set_can_focus(False)
        win.set_deletable(False)

        if self.backend == "layer-shell":
            LayerShell.init_for_window(win)
            LayerShell.set_namespace(win, "kivi")
            LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
            LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
            LayerShell.set_exclusive_zone(win, 0)

        # the pill is painted on a full-window canvas; cards float over it as overlay children
        root = self.root = Gtk.Overlay()
        root.add_css_class("kivi-root")
        root.set_can_focus(False)
        win.set_child(root)

        self.area = Gtk.DrawingArea(content_width=WIN_W, content_height=WIN_H)
        self.area.set_can_focus(False)
        self.area.set_draw_func(self._draw)
        root.set_child(self.area)
        self._build_reply_card()
        self._build_menu_card()

        click = Gtk.GestureClick(button=0)
        click.connect("released", self._on_pill_click)
        self.area.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda c, x, y: self._on_motion(x, y))
        motion.connect("motion", lambda c, x, y: self._on_motion(x, y))
        motion.connect("leave", lambda c: self._on_motion(-1, -1))
        self.area.add_controller(motion)

        root_motion = Gtk.EventControllerMotion()
        root_motion.connect("leave", lambda c: self._later("menu-leave", 700, self._hide_card_if, "menu"))
        root_motion.connect("enter", lambda c, x, y: self._cancel("menu-leave"))
        root.add_controller(root_motion)

        self._apply_position()
        win.connect("realize", lambda w: GLib.idle_add(self._update_region))

    def _apply_position(self):
        for card in (self.reply_card, self.menu_card):
            card.set_valign(Gtk.Align.START if self._position == "top" else Gtk.Align.END)
        if self.backend == "layer-shell":
            top = self._position == "top"
            LayerShell.set_anchor(self.win, LayerShell.Edge.TOP, top)
            LayerShell.set_anchor(self.win, LayerShell.Edge.BOTTOM, not top)
            LayerShell.set_margin(self.win, LayerShell.Edge.TOP if top else LayerShell.Edge.BOTTOM,
                                  self._edge_margin())
        elif self._kwin and self.win.get_realized():
            self._kwin.load(self._position, self._edge_margin())

    def _card_box(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER)
        box.add_css_class("kivi-card")
        box.set_can_focus(False)
        box.set_visible(False)
        self.root.add_overlay(box)
        return box

    def _button(self, label, css, cb) -> Gtk.Button:
        b = Gtk.Button(label=label, focus_on_click=False, can_focus=False)
        b.add_css_class(css)
        b.connect("clicked", cb)
        return b

    def _build_reply_card(self):
        card = self.reply_card = self._card_box()
        card.set_size_request(260, -1)
        card.set_spacing(8)

        head = Gtk.Box(spacing=8)
        lab = Gtk.Label(label="YOU ASKED", xalign=0)
        lab.add_css_class("kivi-asked-label")
        self.asked = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, max_width_chars=36)
        self.asked.add_css_class("kivi-asked")
        head.append(lab)
        head.append(self.asked)
        self.asked_row = head
        card.append(head)

        self.answer = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                max_width_chars=38, lines=9, ellipsize=Pango.EllipsizeMode.END)
        self.answer.add_css_class("kivi-answer")
        self.answer.set_size_request(-1, -1)
        card.append(self.answer)

        foot = Gtk.Box(spacing=6)
        foot.add_css_class("kivi-card-foot")
        brand = Gtk.Label(label="kivi", xalign=0, hexpand=True)
        brand.add_css_class("kivi-brand")
        self.copy_btn = self._button("copy", "kivi-chip", self._on_copy)
        foot.append(brand)
        foot.append(self.copy_btn)
        foot.append(self._button("dismiss", "kivi-ghost", lambda *_: self._hide_card()))
        card.append(foot)

        click = Gtk.GestureClick(button=1)
        click.connect("released", self._on_card_click)
        card.add_controller(click)
        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda *_: self._cancel("card"))
        hover.connect("leave", lambda *_: self._later("card", 2500, self._hide_card_if, "reply"))
        card.add_controller(hover)

    def _build_menu_card(self):
        card = self.menu_card = self._card_box()
        card.add_css_class("kivi-menu")
        card.set_size_request(200, -1)
        head = Gtk.Label(label="kivi", xalign=0)
        head.add_css_class("kivi-menu-head")
        card.append(head)
        self._menu_buttons = {}
        for i, (key, label) in enumerate(MENU_ITEMS):
            if key == "quit":
                card.append(Gtk.Separator())
            b = self._button(label, "kivi-menu-item", lambda _b, k=key: self._on_menu_item(k))
            b.get_child().set_xalign(0)
            if key == "quit":
                b.add_css_class("kivi-danger")
            self._menu_buttons[key] = b
            card.append(b)

    # -- card handling ------------------------------------------------------------------------

    def _show_card(self, which):
        other = self.menu_card if which == "reply" else self.reply_card
        other.set_visible(False)
        card = self.reply_card if which == "reply" else self.menu_card
        card.set_visible(True)
        self._card = which
        self._card_a.to(1.0, self._now_t())
        self._kick()
        GLib.idle_add(self._update_region)

    def _hide_card(self):
        self._cancel("card")
        self._card_a.to(0.0, self._now_t(), 200)
        self._kick()

    def _hide_card_if(self, which):
        if self._card == which and self._card_a.target > 0:
            self._hide_card()

    def _on_card_click(self, gesture, n, x, y):
        picked = self.reply_card.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None and picked is not self.reply_card:
            if isinstance(picked, Gtk.Button):
                return
            picked = picked.get_parent()
        self._hide_card()

    def _on_copy(self, *_):
        text = self.answer.get_text()
        try:
            from .desktop import Clipboard
            Clipboard().set(text)
        except Exception:
            self.win.get_clipboard().set(text)
        self.copy_btn.set_label("copied")
        self._later("copied", 1400, lambda: self.copy_btn.set_label("copy"))

    def _on_menu_item(self, key):
        self._hide_card()
        if key == "pause":
            key = "resume" if self.paused else "pause"
            self._set_paused(key == "pause")
        elif key == "hide":
            self._set_user_hidden(True)
        try:
            self.on_menu(key)
        except Exception as e:
            print(f"kivi: menu handler failed: {e}", file=sys.stderr)

    # -- input --------------------------------------------------------------------------------

    def _pill_rect(self, pad=0.0):
        w, h = self._w.value, self._h.value
        x = (WIN_W - w) / 2
        y = WIN_H - EDGE_PAD - h if self._position == "bottom" else EDGE_PAD
        return x - pad, y - pad, w + 2 * pad, h + 2 * pad

    def _in_pill(self, x, y):
        px, py, pw, ph = self._pill_rect(4)
        return px <= x <= px + pw and py <= y <= py + ph

    def _on_motion(self, x, y):
        hover = self._in_pill(x, y) and self._alpha.target > 0
        if hover != self._hover:
            self._hover = hover
            if self.state == "idle":
                self._later("hover", 0 if not hover else 120, self._set_state, "idle", {})

    def _on_pill_click(self, gesture, n, x, y):
        if not self._in_pill(x, y) or self._alpha.target == 0:
            return
        if gesture.get_current_button() == 3:
            if self._card == "menu" and self._card_a.target > 0:
                self._hide_card()
            else:
                self._show_card("menu")
            return
        if self._card_a.target > 0 and self._card == "menu":
            self._hide_card()
        try:
            self.on_click()
        except Exception as e:
            print(f"kivi: click handler failed: {e}", file=sys.stderr)

    def _update_region(self):
        surface = self.win.get_surface()
        if surface is None:
            return False
        rects = []
        if self._alpha.value > 0.01 or self._alpha.target > 0:
            x, y, w, h = self._pill_rect(6)
            w = max(w, self._w.target + 12)
            x = (WIN_W - w) / 2
            rects.append((int(x), int(y), int(math.ceil(w)), int(math.ceil(h))))
        if self._card_a.target > 0:
            card = self.reply_card if self._card == "reply" else self.menu_card
            ok, b = card.compute_bounds(self.win)
            if ok:
                rects.append((int(b.get_x()), int(b.get_y()), int(b.get_width()), int(b.get_height())))
        key = tuple(rects)
        if key != self._region:
            self._region = key
            surface.set_input_region(cairo.Region([cairo.RectangleInt(*r) for r in rects]))
        return False

    # -- timers -------------------------------------------------------------------------------

    def _later(self, name, ms, fn, *args):
        self._cancel(name)

        def fire():
            self._timers.pop(name, None)
            fn(*args)
            return False
        self._timers[name] = GLib.timeout_add(ms, fire)

    def _cancel(self, name):
        tid = self._timers.pop(name, None)
        if tid:
            GLib.source_remove(tid)

    # -- state machine ------------------------------------------------------------------------

    def _handle(self, event, data):
        if event == "level":
            self._level_target = max(0.0, min(1.0, float(data.get("value", 0.0))))
            self._level_at = time.monotonic()
            return False
        if event == "idle":
            if self.state in ("done", "error"):
                self._pending_idle = True
            else:
                self._set_state("idle", {})
        elif event == "cancelled":
            self._set_state("idle", {})
        elif event == "reply":
            self._show_reply(data.get("text", ""), data.get("heard", ""))
            if self.state != "speaking":
                self._set_state("idle", {})
        elif event in ("listening", "thinking", "heard", "speaking", "done", "error"):
            self._set_state(event, data)
        elif event == "status":   # daemon extra: {"paused", "input", "hint"}
            if "hint" in data and data["hint"] != self.hint:
                self._set_hint(data.get("hint") or "")
            if "paused" in data and bool(data["paused"]) != self.paused:
                self._set_paused(bool(data["paused"]))
        elif event == "hint":
            self._set_hint(data.get("text", ""))
        elif event == "paused":
            self._set_paused(bool(data.get("paused", True)))
        elif event == "overlay":
            self._set_user_hidden(not data.get("visible", True))
        return False

    def _show_reply(self, text, heard):
        text = (text or "").strip()
        if not text:
            return
        self.answer.set_text(text)
        heard = " ".join((heard or "").split())
        self.asked.set_text(f"“{heard}”" if heard else "")
        self.asked_row.set_visible(bool(heard))
        self.copy_btn.set_label("copy")
        self._show_card("reply")
        secs = max(6.0, 2.5 + len(text) / 14)
        self._later("card", int(secs * 1000), self._hide_card_if, "reply")

    def _set_state(self, state, data, refresh=False):
        now = self._now_t()
        prev = self.state
        if not refresh:
            self._cancel("transient")
            self._pending_idle = False
            if state == "listening":
                self._mode = data.get("mode", "dictate")
                self._t_listen = time.monotonic()
                self._level_target = 0.0
                if self._card:
                    self._hide_card()
            elif state in ("thinking",):
                self._mode = data.get("mode", self._mode)
            elif state == "heard":
                self._text = data.get("text", "")
            elif state == "done":
                self._text = data.get("text", "")
                self._later("transient", 1400, self._end_transient)
            elif state == "error":
                self._text = data.get("message", "something went wrong")
                self._later("transient", 3000, self._end_transient)
        self.state = state

        w, h = self._measure()
        shown = self._enabled and (state != "idle" or (self._idle_style != "hidden" and not self._user_hidden))
        self._alpha.to(1.0 if shown else 0.0, now)
        self._w.to(w, now)
        self._h.to(h, now)
        self._glow.to(1.0 if state == "listening" else 0.0, now)
        if not refresh or (prev != state):
            self._content.snap(0.0)
            self._content.to(1.0, now + 0.04)
        self._kick()
        GLib.idle_add(self._update_region)
        return False

    def _end_transient(self):
        if self._pending_idle or self.state in ("done", "error"):
            self._set_state("idle", {})

    # -- measuring ----------------------------------------------------------------------------

    def _layout(self, key, text, font, size, weight=Pango.Weight.NORMAL, max_w=None, spacing=0.0):
        lay = self._layouts.get(key)
        if lay is None:
            lay = self._layouts[key] = self.area.create_pango_layout("")
        desc = Pango.FontDescription.from_string(f"{font} {size}")
        desc.set_weight(weight)
        lay.set_font_description(desc)
        attrs = Pango.AttrList()
        if spacing:
            attrs.insert(Pango.attr_letter_spacing_new(int(spacing * Pango.SCALE)))
        try:
            attrs.insert(Pango.attr_font_features_new("tnum 1"))
        except (AttributeError, TypeError):
            pass
        lay.set_attributes(attrs)
        lay.set_text(text, -1)
        lay.set_ellipsize(Pango.EllipsizeMode.END if max_w else Pango.EllipsizeMode.NONE)
        lay.set_width(int(max_w * Pango.SCALE) if max_w else -1)
        return lay

    @staticmethod
    def _lw(lay):
        return lay.get_pixel_extents()[1].width

    def _measure(self):
        """Lay out the pill's content for the current state; returns its (width, height)."""
        s = self.state
        self._items = []   # (kind, x_offset, payload) drawn left-to-right
        pad_l, pad_r, gap = 16, 18, 11
        if s == "idle":
            if self._hover:
                text = self.hint or ("kivi is paused · right-click to resume" if self.paused else
                                     f"hold {self._dictate_label} to talk · click for agent")
                lay = self._layout("hint", text, FONT_BODY, 9.5, Pango.Weight.MEDIUM)
                self._items = [("bars-small", 12, 14), ("text", 12 + 14 + 9, (lay, "ink-2"))]
                return 12 + 14 + 9 + self._lw(lay) + 14, 30
            self._items = [("bars-idle", 0, 0)]
            return 46, 16

        x = pad_l
        if s in ("listening", "speaking"):
            self._items.append(("bars", x, 27))
            x += 27 + gap
            label = MODE_LABELS.get(self._mode, "listening") if s == "listening" else "speaking"
            lay = self._layout("label", label, FONT_BODY, 10.5, Pango.Weight.MEDIUM)
            self._items.append(("text", x, (lay, "ink")))
            x += self._lw(lay)
            if s == "listening":
                tl = self._layout("timer", "0:00", FONT_MONO, 8.5)
                x += 16
                self._items.append(("timer", x, tl))
                x += self._lw(tl) + 2
            return max(184 if s == "listening" else 160, x + pad_r), 44
        if s == "thinking":
            self._items.append(("wave", x, 27))
            x += 27 + gap
            lay = self._layout("label", "thinking…", FONT_BODY, 10.5, Pango.Weight.MEDIUM)
            self._items.append(("shimmer", x, lay))
            return max(164, x + self._lw(lay) + pad_r + 4), 44
        if s == "heard":
            self._items.append(("wave-dim", x, 27))
            x += 27 + gap
            eb = self._layout("eyebrow", "YOU SAID", FONT_BODY, 7, Pango.Weight.SEMIBOLD, spacing=0.9)
            self._items.append(("eyebrow", x, eb))
            x += self._lw(eb) + 8
            lay = self._layout("body", f"“{' '.join(self._text.split())}”", FONT_BODY, 10.5,
                               max_w=PILL_MAX_W - x - pad_r)
            self._items.append(("text", x, (lay, "ink-2")))
            return min(PILL_MAX_W, x + self._lw(lay) + pad_r), 44
        if s == "done":
            self._items.append(("check", x, 18))
            x += 18 + 10
            text = _clip_text(self._text, 40) or "done"
            lay = self._layout("body", text, FONT_BODY, 10.5, Pango.Weight.MEDIUM, max_w=PILL_MAX_W - x - pad_r)
            self._items.append(("text", x, (lay, "ink")))
            return max(120, min(PILL_MAX_W, x + self._lw(lay) + pad_r)), 44
        if s == "error":
            self._items.append(("error", x, 18))
            x += 18 + 10
            lay = self._layout("body", _clip_text(self._text, 90), FONT_BODY, 10.5, Pango.Weight.MEDIUM,
                               max_w=PILL_MAX_W - x - pad_r)
            self._items.append(("text", x, (lay, "#ff9d91")))
            return max(140, min(PILL_MAX_W, x + self._lw(lay) + pad_r)), 44
        return 46, 16

    # -- animation ----------------------------------------------------------------------------

    def _now_t(self):
        return time.monotonic()

    def _snap(self):
        for t in (self._w, self._h, self._alpha, self._content, self._glow):
            t.snap(t.target)

    def _kick(self):
        if self._tick_id is None:
            self._last_t = time.monotonic()
            self._tick_id = self.area.add_tick_callback(self._tick)

    def _animating_bars(self):
        return self.state in ("listening", "thinking", "heard", "speaking")

    def _tick(self, widget, clock):
        now = time.monotonic()
        dt = min(0.05, max(0.0, now - self._last_t))
        self._last_t = now
        moving = False
        for t in (self._w, self._h, self._alpha, self._content, self._glow):
            moving |= t.step(now)
        if self._card_a.step(now):
            moving = True
        self._apply_card_anim()
        bars_moving = self._step_bars(now, dt)
        if moving:
            self._update_region()
        self.area.queue_draw()
        if moving or bars_moving or self._animating_bars():
            return GLib.SOURCE_CONTINUE
        self._update_region()
        self._tick_id = None
        return GLib.SOURCE_REMOVE

    def _apply_card_anim(self):
        card = self._card
        if not card:
            return
        w = self.reply_card if card == "reply" else self.menu_card
        a = self._card_a.value
        w.set_opacity(a)
        off = int(round(EDGE_PAD + self._h.value + CARD_GAP + (1 - a) * 10))
        w.set_margin_bottom(off if self._position == "bottom" else 0)
        w.set_margin_top(off if self._position == "top" else 0)
        if a <= 0.001 and self._card_a.target == 0:
            w.set_visible(False)
            self._card = None
            GLib.idle_add(self._update_region)

    def _step_bars(self, now, dt):
        """Smooth each bar toward its target; returns True while any bar is still settling."""
        if now - self._level_at > 0.25:
            self._level_target *= 0.0 if now - self._level_at > 0.6 else 0.9
        k = 22.0 if self._level_target > self._level else 7.0
        self._level += (self._level_target - self._level) * (1 - math.exp(-dt * k))
        lv = self._level
        s = self.state
        targets = []
        for i, rest in enumerate(theme.BAR_REST):
            ph, fr = self._phases[i], self._freqs[i]
            if s == "listening":
                wob = 0.5 + 0.5 * math.sin(now * fr + ph)
                breath = 0.04 * math.sin(now * 2.2 + ph)
                v = 0.2 + 0.2 * rest + breath + lv * (0.4 + 0.6 * rest) * (0.5 + 0.5 * wob) * 1.15
            elif s == "speaking":
                env = 0.5 + 0.5 * math.sin(now * 2.6 + ph * 0.4) * math.sin(now * 1.3 + 1.1)
                v = 0.26 + 0.6 * rest * (0.4 + 0.6 * abs(math.sin(now * fr * 0.9 + ph))) * (0.5 + 0.5 * env)
            elif s in ("thinking", "heard"):
                v = 0.3 + 0.42 * (0.5 + 0.5 * math.sin(now * 4.2 - i * 0.75))
            else:
                v = rest
            targets.append(max(0.14, min(1.0, v)))
        rate = 30.0 if s == "listening" else 12.0
        moving = False
        for i, t in enumerate(targets):
            d = t - self._bars[i]
            self._bars[i] += d * (1 - math.exp(-dt * rate))
            moving |= abs(d) > 0.002
        return moving

    # -- drawing ------------------------------------------------------------------------------

    def _draw(self, area, cr, width, height):
        alpha = self._alpha.value
        if alpha <= 0.005:
            return
        x, y, w, h = self._pill_rect()
        r = h / 2
        cr.push_group()
        idle = self.state == "idle" and not self._hover

        # soft cast shadow
        layers = 7
        for i in range(layers, 0, -1):
            spread = i * (1.3 if idle else 1.8)
            a = (0.05 if idle else 0.065) * (1 - (i - 1) / layers)
            cr.set_source_rgba(0.024, 0.04, 0.02, a)
            _rounded(cr, x - spread, y - spread + (1.5 if idle else 3.5), w + 2 * spread, h + 2 * spread,
                     r + spread)
            cr.fill()

        # listening glow ring, like the held keycap
        g = self._glow.value * (0.2 + 0.8 * min(1.0, self._level * 1.5))
        if g > 0.01:
            for spread, a in ((6.0, 0.045), (3.0, 0.07), (1.2, 0.09)):
                cr.set_source_rgba(*theme.rgba("green-glow", a * g))
                _rounded(cr, x - spread, y - spread, w + 2 * spread, h + 2 * spread, r + spread)
                cr.fill()

        # body: dusk gradient, lit from above
        grad = cairo.LinearGradient(0, y, 0, y + h)
        grad.add_color_stop_rgba(0, *theme.rgba("#182015", 0.97 if not idle else 0.86))
        grad.add_color_stop_rgba(1, *theme.rgba("#0d110b", 0.97 if not idle else 0.86))
        _rounded(cr, x, y, w, h, r)
        cr.set_source(grad)
        cr.fill()
        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.10 if not idle else 0.12)
        _rounded(cr, x + 0.5, y + 0.5, w - 1, h - 1, r - 0.5)
        cr.stroke()

        # the edge catch: a specular hairline along the top
        if h > 20:
            hl = cairo.LinearGradient(x, 0, x + w, 0)
            for stop, a in ((0, 0), (0.2, 0.10), (0.5, 0.14), (0.8, 0.10), (1, 0)):
                hl.add_color_stop_rgba(stop, 0.965, 0.953, 0.918, a)
            cr.set_source(hl)
            cr.rectangle(x + r * 0.6, y + 0.5, w - r * 1.2, 1)
            cr.fill()

        # content, clipped to the (possibly still growing) capsule
        cr.save()
        _rounded(cr, x, y, w, h, r)
        cr.clip()
        ca = self._content.value
        cy = y + h / 2
        for kind, dx, payload in self._items:
            self._draw_item(cr, kind, x + dx, cy, payload, ca, x, w)
        cr.restore()

        cr.pop_group_to_source()
        cr.paint_with_alpha(alpha)

    def _draw_bars(self, cr, x0, cy, total_w, bar_h, bar_w, color, scales, a):
        n = len(scales)
        gap = (total_w - n * bar_w) / (n - 1)
        cr.set_source_rgba(*theme.rgba(color, theme.rgba(color)[3] * a))
        for i, s in enumerate(scales):
            hh = max(bar_w, bar_h * s)
            bx = x0 + i * (bar_w + gap)
            _rounded(cr, bx, cy - hh / 2, bar_w, hh, bar_w / 2)
            cr.fill()

    def _draw_item(self, cr, kind, x, cy, payload, ca, px, pw):
        if kind == "bars-idle":
            color = "#c77e28" if self.paused else "#8d8b7e"
            self._draw_bars(cr, px + (pw - 17) / 2, cy, 17, 7.5, 1.6, color, theme.BAR_REST, 0.85)
        elif kind == "bars-small":
            color = "#c77e28" if self.paused else "green-glow"
            self._draw_bars(cr, x, cy, payload, 11, 1.8, color, theme.BAR_REST, ca)
        elif kind == "bars":
            self._draw_bars(cr, x, cy, payload, 20, 2.4, "green-glow", self._bars, ca)
        elif kind == "wave":
            self._draw_bars(cr, x, cy, payload, 18, 2.4, "green-glow", self._bars, ca * 0.9)
        elif kind == "wave-dim":
            self._draw_bars(cr, x, cy, payload, 16, 2.4, "green-glow", self._bars, ca * 0.5)
        elif kind == "text":
            lay, color = payload
            self._text_at(cr, lay, x, cy, color, ca)
        elif kind == "eyebrow":
            self._text_at(cr, payload, x, cy + 0.5, "rgba(244,241,230,0.45)", ca)
        elif kind == "timer":
            secs = int(time.monotonic() - self._t_listen)
            payload.set_text(f"{secs // 60}:{secs % 60:02d}", -1)
            self._text_at(cr, payload, x, cy, "ink-3", ca)
        elif kind == "shimmer":
            _, ext = payload.get_pixel_extents()
            span = ext.width + 120
            cx = x - 60 + ((time.monotonic() * 0.8) % 1.4) / 1.4 * span
            ink2, ink = theme.rgba("ink-3"), theme.rgba("ink")
            g = cairo.LinearGradient(cx - 46, 0, cx + 46, 0)
            g.set_extend(cairo.EXTEND_PAD)
            g.add_color_stop_rgba(0, ink2[0], ink2[1], ink2[2], ca)
            g.add_color_stop_rgba(0.5, ink[0], ink[1], ink[2], ca)
            g.add_color_stop_rgba(1, ink2[0], ink2[1], ink2[2], ca)
            cr.move_to(x, cy - ext.height / 2 - ext.y)
            PangoCairo.layout_path(cr, payload)
            cr.set_source(g)
            cr.fill()
        elif kind == "check":
            d = payload
            cr.set_source_rgba(*theme.rgba("green-glow", ca))
            cr.arc(x + d / 2, cy, d / 2, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*theme.rgba("stage", ca))
            cr.set_line_width(1.9)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.move_to(x + d * 0.29, cy + d * 0.02)
            cr.line_to(x + d * 0.44, cy + d * 0.17)
            cr.line_to(x + d * 0.72, cy - d * 0.15)
            cr.stroke()
        elif kind == "error":
            d = payload
            cr.set_source_rgba(*theme.rgba("error", 0.18 * ca))
            cr.arc(x + d / 2, cy, d / 2, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*theme.rgba("error", ca))
            cr.set_line_width(1.8)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(x + d / 2, cy - d * 0.22)
            cr.line_to(x + d / 2, cy + d * 0.06)
            cr.stroke()
            cr.arc(x + d / 2, cy + d * 0.24, 1.1, 0, 2 * math.pi)
            cr.fill()

    @staticmethod
    def _text_at(cr, lay, x, cy, color, a):
        _, logical = lay.get_pixel_extents()
        col = theme.rgba(color)
        cr.set_source_rgba(col[0], col[1], col[2], col[3] * a)
        cr.move_to(x, cy - logical.height / 2 - logical.y)
        PangoCairo.show_layout(cr, lay)


# ------------------------------------------------------------------------------------- demo

def _demo(argv):
    only = None
    if "--state" in argv:
        only = argv[argv.index("--state") + 1]
    lib = "/usr/lib/libgtk4-layer-shell.so"
    if ("--no-layer-shell" not in argv and os.path.exists(lib) and LAYER_LIB not in os.environ.get("LD_PRELOAD", "")
            and not os.environ.get("KIVI_PRELOADED")):
        env = dict(os.environ, LD_PRELOAD=f"{lib}:{os.environ.get('LD_PRELOAD', '')}".rstrip(":"),
                   KIVI_PRELOADED="1")
        os.execve(sys.executable, [sys.executable, "-m", "kivi.overlay", *argv], env)
    if "--no-layer-shell" in argv and not os.environ.get("KIVI_OVERLAY_BACKEND"):
        os.environ["KIVI_OVERLAY_BACKEND"] = "kwin"

    app = Gtk.Application(application_id="ai.kivi.Linux.OverlayDemo", flags=Gio.ApplicationFlags.NON_UNIQUE)
    cfg = {"overlay": {"enabled": True, "position": "top" if "--top" in argv else "bottom", "idle": "pill",
                       "margin": 28},
           "shortcuts": {"dictate": "RIGHTCTRL"}}
    state = {"ov": None, "level_src": None, "t": 0.0}

    def fake_levels():
        t = state["t"] = state["t"] + 1 / 30
        syll = max(0.0, math.sin(t * 9.0)) * (0.55 + 0.45 * math.sin(t * 1.7))
        word_gap = 0.0 if (t % 2.2) > 1.8 else 1.0
        state["ov"].handle("level", {"value": max(0.0, min(1.0, syll * word_gap * 0.95 + 0.05))})
        return True

    def levels(on):
        if on and state["level_src"] is None:
            state["level_src"] = GLib.timeout_add(33, fake_levels)
        elif not on and state["level_src"] is not None:
            GLib.source_remove(state["level_src"])
            state["level_src"] = None

    steps = [
        ("idle", {}, 2.5),
        ("hover", {}, 2.5),
        ("listening", {"mode": "dictate"}, 4.0),
        ("thinking", {"mode": "dictate"}, 2.5),
        ("heard", {"text": "okay so quick update auth flow is done um tag priya she should review it"}, 2.5),
        ("done", {"mode": "dictate", "text": "Quick update: the auth flow is done. @Priya should review it."}, 2.0),
        ("idle", {}, 1.5),
        ("listening", {"mode": "agent"}, 3.0),
        ("thinking", {"mode": "agent"}, 1.5),
        ("reply", {"heard": "what's the capital of australia and why not sydney",
                   "text": "canberra. it was purpose-built as a compromise, because sydney and melbourne "
                           "both wanted to be the capital."}, 0.6),
        ("speaking", {}, 3.5),
        ("idle", {}, 3.0),
        ("error", {"message": "couldn't reach sarvam — check your connection"}, 3.5),
        ("menu", {}, 3.0),
        ("idle", {}, 2.0),
    ]

    def run(i):
        ov = state["ov"]
        name, data, secs = steps[i % len(steps)] if only is None else next(
            (s for s in steps if s[0] == only), ("idle", {}, 3.0))
        levels(name == "listening")
        ov._hover = name == "hover"
        if name == "hover":
            ov._set_state("idle", {})
        elif name == "menu":
            ov._show_card("menu")
        else:
            ov.handle(name, data)
        print(f"demo: {name}", flush=True)
        if only is None or name in ("done", "error"):
            GLib.timeout_add(int(secs * 1000), run, i + 1)
        return False

    def activate(app):
        app.hold()
        state["ov"] = Overlay(app, cfg, on_click=lambda: print("demo: click"),
                              on_menu=lambda k: (print(f"demo: menu {k}"), k == "quit" and app.quit()))
        print(f"demo: backend={state['ov'].backend}", flush=True)
        GLib.timeout_add(600, run, 0)
        if "--seconds" in argv:
            GLib.timeout_add(int(float(argv[argv.index("--seconds") + 1]) * 1000), app.quit)

    app.connect("activate", activate)
    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        add_signal = GLibUnix.signal_add
    except (ValueError, ImportError):
        add_signal = GLib.unix_signal_add
    for sig in (2, 15):
        add_signal(GLib.PRIORITY_DEFAULT, sig, lambda: (app.quit(), False)[1])
    app.run([sys.argv[0]])


if __name__ == "__main__":
    if "--demo" in sys.argv[1:]:
        _demo(sys.argv[1:])
    else:
        print("usage: python3 -m kivi.overlay --demo [--state NAME] [--top] [--no-layer-shell] [--seconds N]")
