"""Global hold-to-talk shortcuts read straight from evdev (works on Wayland and X11).

The combo logic lives in HotkeyListener._feed(code, value, t) so it can be driven by
synthetic events in tests; run() only reads devices and forwards key events to it.
"""

import selectors
import threading
import time

from evdev import InputDevice, ecodes, list_devices

from .desktop import VIRTUAL_KEYBOARD_NAME

DOUBLE_TAP_GAP = 0.4
RESCAN_SECONDS = 3.0

_LABELS = {"CTRL": "ctrl", "SHIFT": "shift", "ALT": "alt", "META": "super", "CAPSLOCK": "caps lock",
           "PAGEUP": "page up", "PAGEDOWN": "page down", "SCROLLLOCK": "scroll lock", "COMPOSE": "menu",
           "SYSRQ": "print screen", "BACKSPACE": "backspace", "LEFTBRACE": "[", "RIGHTBRACE": "]"}


def _names(s: str) -> list[str]:
    return [p.strip().upper().removeprefix("KEY_") for p in s.split("+") if p.strip()]


def parse_combo(s: str) -> frozenset[int]:
    names = _names(s or "")
    if not names:
        raise ValueError("empty shortcut")
    codes = set()
    for name in names:
        code = ecodes.ecodes.get("KEY_" + name)
        if code is None:
            raise ValueError(f"unknown key {name!r}")
        codes.add(code)
    return frozenset(codes)


def combo_label(s: str) -> str:
    out = []
    for name in _names(s or ""):
        side = ""
        for prefix in ("LEFT", "RIGHT"):
            rest = name.removeprefix(prefix)
            if rest != name and rest in ("CTRL", "SHIFT", "ALT", "META"):
                side, name = prefix.lower() + " ", rest
        out.append(side + _LABELS.get(name, name.lower()))
    return " + ".join(out)


class HotkeyListener(threading.Thread):
    def __init__(self, shortcuts: dict[str, str], on_start, on_change, on_end, *, min_hold=0.35, hands_free=True):
        super().__init__(name="kivi-hotkeys", daemon=True)
        self.on_start, self.on_change, self.on_end = on_start, on_change, on_end
        self.min_hold = min_hold
        self.hands_free = hands_free
        self.devices: list[str] = []
        self.errors: dict[str, str] = {}  # action -> why its shortcut was rejected
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._combos: dict[str, frozenset[int]] = {}
        self._pressed: set[int] = set()
        self._active = None  # action currently held (or latched)
        self._t0 = 0.0
        self._cancelled = False
        self._upgraded = False
        self._latched = False
        self._double_candidate = False
        self._last_tap_end = None
        self.update(shortcuts)

    # ------------------------------------------------------------ config

    def update(self, shortcuts, *, min_hold=None, hands_free=None):
        combos, errors = {}, {}
        for action, spec in (shortcuts or {}).items():
            try:
                combos[action] = parse_combo(spec)
            except ValueError as e:
                errors[action] = str(e)
        with self._lock:
            self._combos = combos
            self.errors = errors
            if min_hold is not None:
                self.min_hold = min_hold
            if hands_free is not None:
                self.hands_free = hands_free
            self._last_tap_end = None

    def stop(self):
        self._stopping.set()

    # ------------------------------------------------------------ state machine

    def _emit(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            print(f"kivi: hotkey callback failed: {e!r}")

    def _exact_match(self):
        """The action whose combo is exactly the set of held keys (longest wins on ties)."""
        hits = [(len(c), a) for a, c in self._combos.items() if c == self._pressed]
        return max(hits)[1] if hits else None

    def _upgrade_for(self, current):
        """A longer combo containing the current one that is now fully held, if any."""
        base = self._combos.get(current, frozenset())
        hits = [(len(c), a) for a, c in self._combos.items()
                if a != current and c > base and c <= self._pressed]
        return max(hits)[1] if hits else None

    def _end(self, cancelled):
        action, self._active = self._active, None
        self._latched = False
        self._emit(self.on_end, action, cancelled)

    def _feed(self, code, value, t):
        if value == 2:  # autorepeat
            return
        with self._lock:
            if value == 1:
                self._press(code, t)
            elif value == 0:
                self._release(code, t)

    def _press(self, code, t):
        self._pressed.add(code)
        if self._latched:
            if self._combos.get(self._active, frozenset()) <= self._pressed:
                self._end(False)  # tap again to finish hands-free dictation
            elif code == ecodes.KEY_ESC:
                self._end(True)
            return
        if self._active:
            upgrade = self._upgrade_for(self._active)
            if upgrade:
                self._active = upgrade
                self._upgraded = True
                self._emit(self.on_change, upgrade)
            elif not any(code in c and self._combos.get(self._active, frozenset()) <= c
                         for c in self._combos.values()):
                self._cancelled = True  # a normal shortcut such as right ctrl + c
            return
        action = self._exact_match()
        if action:
            self._active, self._t0 = action, t
            self._cancelled = self._upgraded = False
            self._double_candidate = (action == "dictate" and self._last_tap_end is not None
                                      and t - self._last_tap_end <= DOUBLE_TAP_GAP)
            self._last_tap_end = None
            self._emit(self.on_start, action)
        elif code not in self._combos.get("dictate", frozenset()):
            self._last_tap_end = None

    def _release(self, code, t):
        self._pressed.discard(code)
        if not self._active or self._latched or code not in self._combos.get(self._active, frozenset()):
            return
        held = t - self._t0
        is_tap = (held < self.min_hold and not self._cancelled and not self._upgraded
                  and self._active == "dictate")
        if is_tap and self.hands_free and self._double_candidate:
            self._latched = True  # recording started on this second press keeps going
            self._double_candidate = False
            return
        self._last_tap_end = t if is_tap and self.hands_free else None
        self._end(self._cancelled or held < self.min_hold)

    def _reset_keys(self):
        """Forget held keys (a device vanished, so its releases will never arrive)."""
        with self._lock:
            self._pressed.clear()
            if self._active and not self._latched:
                self._end(True)

    # ------------------------------------------------------------ devices

    def _wanted_codes(self):
        with self._lock:
            return set().union(*self._combos.values()) if self._combos else set()

    def _scan(self, opened: dict, sel):
        wanted = self._wanted_codes()
        for path in list_devices():
            if path in opened:
                continue
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
            if dev.name == VIRTUAL_KEYBOARD_NAME or ecodes.KEY_A not in keys or not (wanted & keys or not wanted):
                dev.close()
                continue
            opened[path] = dev
            sel.register(dev, selectors.EVENT_READ)
        self.devices = sorted({d.name for d in opened.values()})

    def _drop(self, opened, sel, dev):
        try:
            sel.unregister(dev)
        except (KeyError, ValueError):
            pass
        opened.pop(dev.path, None)
        try:
            dev.close()
        except OSError:
            pass
        self.devices = sorted({d.name for d in opened.values()})
        self._reset_keys()

    def run(self):
        sel = selectors.DefaultSelector()
        opened: dict[str, InputDevice] = {}
        next_scan = 0.0
        try:
            while not self._stopping.is_set():
                now = time.monotonic()
                if now >= next_scan:
                    self._scan(opened, sel)
                    next_scan = now + RESCAN_SECONDS
                if not opened:
                    self._stopping.wait(0.5)
                    continue
                for key, _ in sel.select(timeout=0.5):
                    dev = key.fileobj
                    try:
                        for ev in dev.read():
                            if ev.type == ecodes.EV_KEY:
                                self._feed(ev.code, ev.value, ev.timestamp())
                    except BlockingIOError:
                        continue
                    except OSError:  # unplugged
                        self._drop(opened, sel, dev)
        finally:
            for dev in list(opened.values()):
                try:
                    dev.close()
                except OSError:
                    pass
            sel.close()
            self.devices = []
