"""System tray icon: a StatusNotifierItem + com.canonical.dbusmenu, spoken directly over Gio D-Bus.

Works with any SNI host (KDE Plasma, waybar, GNOME with the AppIndicator extension). No extra
dependencies: the icon is drawn with cairo in the kivi palette and sent as ARGB32 pixmaps.

    tray = Tray(on_action)          # from the GTK/GLib main thread
    tray.set_state("listening")     # any thread
    tray.set_paused(True); tray.set_pill_visible(False); tray.close()

on_action(key) keys: agent (left click), dictate (middle click), settings, pause, resume,
pill-show, pill-hide, quit.  Run `python3 -m kivi.tray --demo` to try it.
"""

from __future__ import annotations

import itertools
import logging
import math
import os
from typing import Callable

from gi.repository import Gio, GLib

from kivi.theme import BAR_REST, rgba

log = logging.getLogger("kivi.tray")

WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
STATES = ("idle", "listening", "thinking", "speaking", "paused", "error")
LABELS = {"idle": "idle", "listening": "listening…", "thinking": "thinking…",
          "speaking": "speaking…", "paused": "paused", "error": "something went wrong"}
SIZES = (22, 32, 48)
ERROR_HOLD_MS = 4000   # keep the error look this long even if "idle" follows right away

_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""

_instances = itertools.count(1)


# ------------------------------------------------------------------ icon drawing

def _draw(cr, size, state):
    """Dark rounded square with five bars, varied per state."""
    s = float(size)
    r = s * 0.22
    cr.new_sub_path()
    cr.arc(s - r, r, r, -math.pi / 2, 0)
    cr.arc(s - r, s - r, r, 0, math.pi / 2)
    cr.arc(r, s - r, r, math.pi / 2, math.pi)
    cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.set_source_rgba(*rgba("stage"))
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, 0.10)
    cr.set_line_width(max(1.0, s / 32))
    cr.stroke()

    heights = {
        "listening": (0.55, 0.95, 0.70, 1.0, 0.62),
        "speaking": (0.70, 0.50, 0.95, 0.60, 0.80),
        "thinking": (0.50, 0.50, 0.50, 0.50, 0.50),
    }.get(state, BAR_REST)
    green, amber, ink = rgba("green-glow"), rgba("amber"), rgba("ink")
    if state == "listening" or state == "speaking":
        colors = [green] * 5
    elif state == "thinking":
        colors = [green, amber, green, amber, green]
    elif state == "paused":
        colors = [rgba("ink-3")] * 5
    elif state == "error":
        colors = [rgba("ink", 0.45)] * 5
    else:  # idle
        colors = [rgba("ink", 0.72)] * 5

    pad = s * 0.19
    span = s - 2 * pad
    bw = span / 7.4           # 5 bars + 4 gaps of 0.6 bar
    max_h = s * 0.56
    cy = s / 2
    cr.set_line_cap(1)  # cairo.LINE_CAP_ROUND
    cr.set_line_width(bw)
    for i, (h, col) in enumerate(zip(heights, colors)):
        x = pad + bw * (1.6 * i + 0.5)
        half = max(bw / 2, h * max_h / 2 - bw / 2)
        cr.set_source_rgba(*col)
        cr.move_to(x, cy - half)
        cr.line_to(x, cy + half)
        cr.stroke()

    if state == "paused":
        lw = max(1.5, s / 11)
        cr.set_line_width(lw + max(1.0, s / 16))
        cr.set_source_rgba(*rgba("stage"))
        cr.move_to(s * 0.26, s * 0.74)
        cr.line_to(s * 0.74, s * 0.26)
        cr.stroke()
        cr.set_line_width(lw)
        cr.set_source_rgba(*rgba("ink-2"))
        cr.move_to(s * 0.26, s * 0.74)
        cr.line_to(s * 0.74, s * 0.26)
        cr.stroke()
    elif state == "error":
        dr = s * 0.17
        cr.arc(s - dr * 1.25, dr * 1.25, dr + max(1.0, s / 24), 0, 2 * math.pi)
        cr.set_source_rgba(*rgba("stage"))
        cr.fill()
        cr.arc(s - dr * 1.25, dr * 1.25, dr, 0, 2 * math.pi)
        cr.set_source_rgba(*rgba("error"))
        cr.fill()


def render_png(state, size, path):
    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    _draw(cairo.Context(surf), size, state)
    surf.write_to_png(path)


def _pixmap(state, size):
    """(w, h, bytes) as SNI wants: non-premultiplied ARGB32, network byte order."""
    import sys

    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    _draw(cairo.Context(surf), size, state)
    surf.flush()
    stride = surf.get_stride()
    data = bytes(surf.get_data())
    out = bytearray(size * size * 4)
    little = sys.byteorder == "little"
    o = 0
    for y in range(size):
        row = y * stride
        for x in range(size):
            i = row + x * 4
            if little:
                b, g, r, a = data[i], data[i + 1], data[i + 2], data[i + 3]
            else:
                a, r, g, b = data[i], data[i + 1], data[i + 2], data[i + 3]
            if 0 < a < 255:  # cairo is premultiplied
                r, g, b = (min(255, (c * 255 + a // 2) // a) for c in (r, g, b))
            out[o:o + 4] = bytes((a, r, g, b))
            o += 4
    return size, size, bytes(out)


_pixmap_cache: dict[str, list] = {}


def _pixmaps(state):
    if state not in _pixmap_cache:
        _pixmap_cache[state] = [_pixmap(state, n) for n in SIZES]
    return _pixmap_cache[state]


_variant_cache: dict[str, GLib.Variant] = {}


def _pixmap_variant(state):
    if state not in _variant_cache:
        _variant_cache[state] = GLib.Variant("a(iiay)", _pixmaps(state))
    return _variant_cache[state]


# ------------------------------------------------------------------ the tray

class Tray:
    # (id, key, label, icon-name); key None = header, "-" = separator
    _ITEMS = [
        (1, None, "kivi", ""),
        (2, "-", "", ""),
        (3, "agent", "talk to kivi", "audio-input-microphone"),
        (4, "dictate", "start dictation", "insert-text"),
        (5, "-", "", ""),
        (6, "settings", "settings", "preferences-system"),
        (7, "pause", "pause kivi", "media-playback-pause"),
        (8, "pill", "hide pill", "view-hidden"),
        (9, "-", "", ""),
        (10, "quit", "quit", "application-exit"),
    ]

    def __init__(self, on_action: Callable[[str], None], icon_name: str = "kivi"):
        self.on_action = on_action
        self.icon_name = icon_name
        self.available = False
        self.state = "idle"
        self.paused = False
        self.pill_visible = True
        self.revision = 1
        self.bus = None
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-{next(_instances)}"
        self._reg_ids: list[int] = []
        self._sub_id = 0
        self._owned = False
        self._watcher_up = False
        self._reg_serial = 0
        self._err_until = 0
        self._err_timer = 0
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            sni = Gio.DBusNodeInfo.new_for_xml(_SNI_XML).interfaces[0]
            menu = Gio.DBusNodeInfo.new_for_xml(_MENU_XML).interfaces[0]
            self._reg_ids.append(self.bus.register_object(ITEM_PATH, sni, self._sni_call, self._sni_prop, None))
            self._reg_ids.append(self.bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_prop, None))
            res = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                     "RequestName", GLib.Variant("(su)", (self.bus_name, 4)),
                                     GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None)
            self._owned = res.unpack()[0] in (1, 4)
            self._sub_id = self.bus.signal_subscribe(
                "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged", "/org/freedesktop/DBus",
                WATCHER, Gio.DBusSignalFlags.NONE, self._on_owner_changed)
            has = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                     "NameHasOwner", GLib.Variant("(s)", (WATCHER,)),
                                     GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
            if has:
                self._watcher_up = True
                self.available = True   # optimistic; reset if the async registration fails
                self._register()
            else:
                log.info("no StatusNotifierWatcher on the bus; tray icon waits for one")
        except Exception as ex:
            log.warning("tray unavailable: %s", ex)
            self.close()

    # ---------------------------------------------------------- public api (any thread)

    def set_state(self, state: str):
        GLib.idle_add(self._set_state, state if state in STATES else "idle")

    def set_paused(self, paused: bool):
        GLib.idle_add(self._set_paused, bool(paused))

    def set_pill_visible(self, visible: bool):
        GLib.idle_add(self._set_pill, bool(visible))

    def close(self):
        if self._err_timer:
            GLib.source_remove(self._err_timer)
            self._err_timer = 0
        if self.bus is None:
            return
        for rid in self._reg_ids:
            try:
                self.bus.unregister_object(rid)
            except Exception:
                pass
        self._reg_ids = []
        if self._sub_id:
            self.bus.signal_unsubscribe(self._sub_id)
            self._sub_id = 0
        if self._owned:
            try:
                self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                   "ReleaseName", GLib.Variant("(s)", (self.bus_name,)), None,
                                   Gio.DBusCallFlags.NONE, -1, None)
            except Exception:
                pass
            self._owned = False
        self.available = False
        self.bus = None

    # ---------------------------------------------------------- watcher registration

    def _register(self):
        """Always async: a watcher may call back into our objects before replying, and our
        handlers run on this (main) context, so a blocking call here could deadlock."""
        if self.bus is None or not self._owned:
            self.available = False
            return
        self._reg_serial += 1
        serial = self._reg_serial

        def done(bus, res, *_):
            if serial != self._reg_serial or self.bus is None:
                return  # superseded by a newer watcher, or closed meanwhile
            try:
                bus.call_finish(res)
                self.available = True
                log.info("tray icon registered as %s", self.bus_name)
            except GLib.Error as ex:
                self.available = False
                log.warning("tray registration failed: %s", ex.message)
        self.bus.call(WATCHER, WATCHER_PATH, WATCHER, "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, 5000, None, done)

    def _on_owner_changed(self, _bus, _sender, _path, _iface, _signal, params, *_):
        name, _old, new = params.unpack()
        if name != WATCHER:
            return
        if new:
            log.info("StatusNotifierWatcher appeared; registering tray icon")
            self._watcher_up = True
            self._register()
        else:
            self._watcher_up = False
            self._reg_serial += 1
            self.available = False

    # ---------------------------------------------------------- state

    def _shown_state(self):
        if self.paused and self.state == "idle":
            return "paused"
        return self.state

    def _status(self):
        return "NeedsAttention" if self.state == "listening" else "Active"

    def _emit(self, path, iface, signal, params=None):
        if self.bus is None:
            return
        try:
            self.bus.emit_signal(None, path, iface, signal, params)
        except GLib.Error as ex:
            log.debug("tray signal %s: %s", signal, ex.message)

    def _set_state(self, state):
        if self._err_timer:
            GLib.source_remove(self._err_timer)
            self._err_timer = 0
        if state == "error":
            self._err_until = GLib.get_monotonic_time() + ERROR_HOLD_MS * 1000
        elif state == "idle" and self.state == "error":
            left = (self._err_until - GLib.get_monotonic_time()) // 1000
            if left > 0:  # engine emits idle right after error; let the error be seen first
                self._err_timer = GLib.timeout_add(int(left), self._error_expired)
                return False
        if state == self.state:
            return False
        old_status = self._status()
        self.state = state
        self._icons_changed(old_status)
        return False

    def _error_expired(self):
        self._err_timer = 0
        self._set_state("idle")
        return False

    def _icons_changed(self, old_status):
        sni = "org.kde.StatusNotifierItem"
        self._emit(ITEM_PATH, sni, "NewIcon")
        self._emit(ITEM_PATH, sni, "NewAttentionIcon")
        self._emit(ITEM_PATH, sni, "NewToolTip")
        if self._status() != old_status:
            self._emit(ITEM_PATH, sni, "NewStatus", GLib.Variant("(s)", (self._status(),)))

    def _set_paused(self, paused):
        if paused != self.paused:
            self.paused = paused
            self._icons_changed(self._status())
            self._menu_changed([7])
        return False

    def _set_pill(self, visible):
        if visible != self.pill_visible:
            self.pill_visible = visible
            self._menu_changed([8])
        return False

    def _menu_changed(self, ids):
        self.revision += 1
        props = [(i, self._item_props(i, [])) for i in ids]
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "ItemsPropertiesUpdated",
                   GLib.Variant("(a(ia{sv})a(ias))", (props, [])))
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                   GLib.Variant("(ui)", (self.revision, 0)))

    # ---------------------------------------------------------- StatusNotifierItem

    def _tooltip(self):
        return ("", [], f"kivi · {LABELS[self._shown_state()]}", "")

    def _sni_prop(self, _bus, _sender, _path, _iface, name):
        shown = self._shown_state()
        v = {
            "Category": ("s", "ApplicationStatus"),
            "Id": ("s", "kivi"),
            "Title": ("s", "kivi"),
            "Status": ("s", self._status()),
            "WindowId": ("i", 0),
            "IconName": ("s", ""),          # pixmaps carry the per-state look
            "AttentionIconName": ("s", ""),
            "OverlayIconName": ("s", ""),
            "AttentionMovieName": ("s", ""),
            "IconThemePath": ("s", ""),
            "ToolTip": ("(sa(iiay)ss)", self._tooltip()),
            "ItemIsMenu": ("b", False),
            "Menu": ("o", MENU_PATH),
        }
        if name in ("IconPixmap", "AttentionIconPixmap"):
            return _pixmap_variant(shown)
        if name == "OverlayIconPixmap":
            return GLib.Variant("a(iiay)", [])
        if name in v:
            return GLib.Variant(*v[name])
        return None

    def _fire(self, key):
        try:
            self.on_action(key)
        except Exception:
            log.exception("tray action %r failed", key)
        return False

    def _sni_call(self, _bus, _sender, _path, _iface, method, _params, invocation):
        if method == "Activate":
            GLib.idle_add(self._fire, "agent")
        elif method == "SecondaryActivate":
            GLib.idle_add(self._fire, "dictate")
        # ContextMenu / Scroll: hosts render our dbusmenu themselves; nothing to do
        invocation.return_value(None)

    # ---------------------------------------------------------- dbusmenu

    def _item(self, item_id):
        for it in self._ITEMS:
            if it[0] == item_id:
                return it
        return None

    def _item_props(self, item_id, names):
        if item_id == 0:
            props = {"children-display": GLib.Variant("s", "submenu")}
        else:
            it = self._item(item_id)
            if it is None:
                return {}
            _id, key, label, icon = it
            if key == "-":
                props = {"type": GLib.Variant("s", "separator")}
            else:
                if key == "pause":
                    label, icon = (("resume kivi", "media-playback-start") if self.paused
                                   else ("pause kivi", "media-playback-pause"))
                elif key == "pill":
                    label, icon = (("hide pill", "view-hidden") if self.pill_visible
                                   else ("show pill", "view-visible"))
                props = {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", key is not None),
                         "visible": GLib.Variant("b", True)}
                if icon:
                    props["icon-name"] = GLib.Variant("s", icon)
        if names:
            props = {k: v for k, v in props.items() if k in names}
        return props

    def _layout(self, item_id, depth, names):
        children = []
        if item_id == 0 and depth != 0:
            children = [GLib.Variant("(ia{sv}av)", self._layout(it[0], depth - 1, names)) for it in self._ITEMS]
        return (item_id, self._item_props(item_id, names), children)

    def _menu_event(self, item_id, event_id):
        if event_id != "clicked":
            return
        it = self._item(item_id)
        if it is None or it[1] in (None, "-"):
            return
        key = it[1]
        if key == "pause":
            key = "resume" if self.paused else "pause"
        elif key == "pill":
            key = "pill-hide" if self.pill_visible else "pill-show"
        GLib.idle_add(self._fire, key)

    def _menu_call(self, _bus, _sender, _path, _iface, method, params, invocation):
        p = params.unpack()
        if method == "GetLayout":
            parent, depth, names = p
            if parent != 0 and self._item(parent) is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error.InvalidId", f"no item {parent}")
                return
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(parent, depth, names))))
        elif method == "GetGroupProperties":
            ids, names = p
            if not ids:
                ids = [0] + [it[0] for it in self._ITEMS]
            out = [(i, self._item_props(i, names)) for i in ids if i == 0 or self._item(i)]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            item_id, name = p
            props = self._item_props(item_id, [name])
            if name not in props:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error.InvalidProperty", name)
                return
            invocation.return_value(GLib.Variant("(v)", (props[name],)))
        elif method == "Event":
            item_id, event_id, _data, _ts = p
            if item_id != 0 and self._item(item_id) is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error.InvalidId", f"no item {item_id}")
                return
            self._menu_event(item_id, event_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            errors = []
            for item_id, event_id, _data, _ts in p[0]:
                if item_id != 0 and self._item(item_id) is None:
                    errors.append(item_id)
                else:
                    self._menu_event(item_id, event_id)
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
        elif method == "AboutToShow":
            if p[0] != 0 and self._item(p[0]) is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error.InvalidId", f"no item {p[0]}")
                return
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            ids = p[0]
            errors = [i for i in ids if i != 0 and self._item(i) is None]
            invocation.return_value(GLib.Variant("(aiai)", ([], errors)))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _menu_prop(self, _bus, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)


# ------------------------------------------------------------------ demo

def _demo(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m kivi.tray")
    ap.add_argument("--demo", action="store_true", help="cycle states every 2 s")
    ap.add_argument("--seconds", type=float, default=0, help="quit after N seconds")
    ap.add_argument("--state", help="hold one state instead of cycling")
    ap.add_argument("--png", help="write every state's icon as PNGs with this path prefix, then exit")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    if a.png:
        for st in STATES:
            for n in SIZES:
                render_png(st, n, f"{a.png}{st}-{n}.png")
        print("wrote", len(STATES) * len(SIZES), "pngs")
        return 0

    loop = GLib.MainLoop()
    tray = None

    def on_action(key):
        print("action:", key, flush=True)
        if key in ("pause", "resume"):
            tray.set_paused(key == "pause")
        elif key in ("pill-hide", "pill-show"):
            tray.set_pill_visible(key == "pill-show")
        elif key == "quit":
            loop.quit()

    tray = Tray(on_action)
    print(f"tray: available={tray.available} name={tray.bus_name}", flush=True)
    if a.state:
        tray.set_state(a.state)
    elif a.demo:
        cycle = itertools.cycle(STATES)

        def step():
            st = next(cycle)
            print("state:", st, flush=True)
            tray.set_state(st)
            return True
        step()
        GLib.timeout_add(2000, step)
    if a.seconds:
        GLib.timeout_add(int(a.seconds * 1000), loop.quit)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    tray.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_demo(sys.argv[1:]))
