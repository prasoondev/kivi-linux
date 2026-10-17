"""The kivi daemon: a windowless Gtk.Application wiring hotkeys, engine, overlay and IPC together."""

import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

log = logging.getLogger("kivi")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIVI_BIN = os.path.join(ROOT, "bin", "kivi")
LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"
PRELOAD_GUARD = "KIVI_PRELOADED"
APP_ID = "ai.kivi.Linux.Daemon"

NO_INPUT_HINT = ("no keyboard access, so hotkeys are off. run `sudo usermod -aG input $USER` and log in again. "
                 "until then: click the pill, or bind `kivi toggle` to a shortcut in system settings.")
NO_INPUT_SHORT = "no keyboard access · click to talk"
CAPTURE_TIMEOUT = 120.0  # settings' shortcut dialog pauses hotkeys at most this long
# engine ui event → tray icon state (the tray holds the error look for a few seconds itself)
TRAY_STATES = {"listening": "listening", "thinking": "thinking", "heard": "thinking", "speaking": "speaking",
               "error": "error", "idle": "idle", "done": "idle", "cancelled": "idle"}


def _preload_layer_shell():
    """gtk4-layer-shell must be loaded before libwayland-client, so re-exec ourselves with LD_PRELOAD."""
    if os.environ.get(PRELOAD_GUARD) or not os.path.exists(LAYER_SHELL_LIB):
        return
    preload = os.environ.get("LD_PRELOAD", "")
    if LAYER_SHELL_LIB in preload.split():
        return
    env = dict(os.environ, LD_PRELOAD=f"{LAYER_SHELL_LIB} {preload}".strip(), **{PRELOAD_GUARD: "1"})
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(sys.executable, sys.orig_argv, env)
    except OSError as ex:
        log.warning("could not re-exec with layer-shell preloaded: %s", ex)


class Notifier:
    """notify-send that replaces its previous bubble instead of stacking new ones.

    Bubbles are sent from a background thread (notify-send can take seconds) and marked
    transient, so dictated text doesn't pile up in Plasma's notification history.
    """

    def __init__(self):
        self.ok = bool(shutil.which("notify-send"))
        self.id = None
        self._q = queue.Queue()
        self._thread = None

    def _send(self, summary, body, timeout):
        cmd = ["notify-send", "-a", "kivi", "-i", "kivi", "-p", "-t", str(timeout),
               "-h", "string:x-kde-origin-name:kivi", "-h", "boolean:transient:true"]
        if self.id:
            cmd += ["-r", self.id]
        try:
            out = subprocess.run(cmd + ["--", summary, body], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return
        self.id = out.strip() or self.id

    def _worker(self):
        while True:
            self._send(*self._q.get())

    def show(self, summary, body="", timeout=2500, wait=False):
        if not self.ok:
            return
        if wait:
            self._send(summary, body, timeout)
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="kivi-notify", daemon=True)
            self._thread.start()
        self._q.put((summary, body, timeout))


MODE_LABELS = {"dictate": "listening…", "command": "editing…", "agent": "kivi agent · listening…"}


class Daemon:
    def __init__(self):
        from gi.repository import Gio, Gtk

        from kivi import config

        self.cfg = config.load_config()
        flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
        self.app = Gtk.Application(application_id=APP_ID, flags=flags)
        self.app.connect("startup", self._startup)
        self.app.connect("activate", lambda _app: None)
        self.app.connect("shutdown", self._shutdown)
        self.overlay = None
        self.tray = None
        self.engine = None
        self.listener = None
        self.server = None
        self.notifier = Notifier()
        self.paused = False
        self.capture_until = 0.0  # settings is recording a new shortcut: hotkeys off until then
        self.held = None          # (engine run, listener session) of the hotkey take driving the engine
        self._held_lock = threading.Lock()
        self._actions = queue.Queue()  # hotkey → engine calls, so the listener thread never blocks
        self.input_ok = False

    def run(self):
        return self.app.run([sys.argv[0]])

    # ------------------------------------------------------------ lifecycle

    def _startup(self, app):
        from gi.repository import GLib

        from kivi import desktop, ipc
        from kivi.engine import Engine

        from kivi import config

        app.hold()
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal, sig)
        config.clean_temp()
        threading.Thread(target=self._action_worker, name="kivi-actions", daemon=True).start()

        self._ensure_overlay()
        try:
            from kivi.tray import Tray
            self.tray = Tray(self._on_tray)
            self.tray.set_pill_visible(self._pill_shown())
        except Exception as ex:
            self.tray = None
            log.warning("tray icon unavailable: %s", ex)
        try:
            self.engine = Engine(self.cfg, self._ui)
        except Exception as ex:
            log.error("could not start the engine: %s", ex)
            self.notifier.show("kivi could not start", str(ex)[:200], 8000)
            GLib.idle_add(app.quit)
            return

        self.input_ok = desktop.input_access_ok()
        if not self.input_ok:
            log.warning(NO_INPUT_HINT)
        if not desktop.uinput_ok():
            log.warning("cannot open /dev/uinput: kivi can't paste into apps until it can")
        self._start_listener()

        try:
            self.server = ipc.Server(self._ipc)
            self.server.start()
        except OSError as ex:
            self.server = None
            log.error("ipc socket unavailable (kivi toggle/status won't work): %s", ex)

        log.info("kivi is running (pill: %s, hotkeys: %s)", getattr(self.overlay, "backend", "none"),
                 "on" if self.listener is not None and self.input_ok else "off")
        self._push_status()
        if not self.cfg.get("api_key"):
            self._ui("error", {"message": "no sarvam api key yet. open kivi settings → api key."})
        elif not self.input_ok:
            self._ui("error", {"message": "no keyboard access. click the pill to talk, or see kivi settings."})

    def _start_listener(self):
        try:
            from kivi.hotkeys import HotkeyListener
            self.listener = HotkeyListener(
                self.cfg["shortcuts"], self._on_start, self._on_change, self._on_end,
                min_hold=self.cfg["min_hold"], hands_free=self.cfg["hands_free"])
            self.listener.start()
        except Exception as ex:  # bad combo in config, evdev missing, ...
            self.listener = None
            log.error("hotkeys disabled: %s", ex)
            self._ui("error", {"message": f"hotkeys off: {ex}"})

    def _shutdown(self, _app):
        if self.tray is not None:
            self.tray.close()
        for stop in (getattr(self.listener, "stop", None), getattr(self.server, "stop", None)):
            if stop:
                try:
                    stop()
                except Exception as ex:
                    log.debug("shutdown: %s", ex)
        try:
            from kivi.audio import stop_playback
            stop_playback()
        except Exception:
            pass
        if self.notifier.id:  # don't leave a stale "listening…" bubble behind
            self.notifier.show("kivi", "stopped", 1, wait=True)
        log.info("kivi stopped")

    def _on_signal(self, sig):
        log.info("got %s, quitting", signal.Signals(sig).name)
        self.app.quit()
        return False

    def quit(self):
        from gi.repository import GLib
        GLib.idle_add(self.app.quit)

    # ------------------------------------------------------------ overlay & ui events

    def _ensure_overlay(self):
        if self.overlay is not None or not self.cfg["overlay"].get("enabled", True):
            return
        try:
            from kivi.overlay import Overlay
            self.overlay = Overlay(self.app, self._overlay_cfg(), self._on_click, self._on_menu)
        except Exception as ex:
            self.overlay = None
            log.error("pill unavailable, using notifications: %s", ex)

    def _overlay_cfg(self):
        return dict(self.cfg, overlay=dict(self.cfg["overlay"]))

    def _overlay_live(self):
        return self.overlay is not None and self.cfg["overlay"].get("enabled", True)

    def _want_notifications(self):
        """Notifications stand in for the pill only when it can't be seen.

        Pill working: never. Pill turned off by the user: if "notifications" is on.
        Pill wanted but broken (no layer-shell/KWin): always, or kivi would be invisible.
        """
        if self._overlay_live() and getattr(self.overlay, "available", True):
            return False
        if not self.cfg["overlay"].get("enabled", True):
            return bool(self.cfg.get("notify"))
        return True

    def _ui(self, event, data):
        """Engine → overlay (thread-safe) plus notification fallback. Called from any thread."""
        if event == "error":
            log.error("%s", data.get("message", ""))
        if self.tray is not None and event in TRAY_STATES:
            self.tray.set_state(TRAY_STATES[event])
        if self._overlay_live():
            try:
                self.overlay.handle(event, data)
            except Exception as ex:
                log.error("overlay: %s", ex)
        if event in ("thinking", "idle", "cancelled"):
            self._check_held()
        if event != "level" and self._want_notifications():
            self._notify(event, data)

    def _notify(self, event, data):
        n = self.notifier.show
        if event == "listening":
            n("kivi", MODE_LABELS.get(data.get("mode"), "listening…"), 30000)
        elif event == "thinking":
            n("kivi", "thinking…", 30000)
        elif event == "done":
            n("kivi", (data.get("text") or "done")[:120], 1500)
        elif event == "reply":
            n(f"you asked: {data.get('heard', '')}"[:80], data.get("text", ""), 15000)
        elif event == "error":
            n("kivi", data.get("message", "something went wrong")[:200], 5000)
        elif event == "cancelled":
            n("kivi", "cancelled", 1)

    def _push_status(self):
        """Extra overlay event (ignored by overlays that don't know it): pause state + tooltip hint."""
        if self._overlay_live():
            hint = "" if self.input_ok else NO_INPUT_SHORT
            tip = "" if self.input_ok else NO_INPUT_HINT
            try:
                self.overlay.handle("status", {"paused": self.paused, "input": self.input_ok, "hint": hint,
                                               "tooltip": tip})
            except Exception as ex:
                log.debug("overlay status: %s", ex)

    def _on_click(self):
        self.engine.toggle("agent")

    def _on_tray(self, key):
        """Tray icon actions; runs on the GLib main loop."""
        if key in ("agent", "dictate"):
            if self.engine is not None and not self.paused:
                self.engine.toggle(key)
        elif key == "settings":
            self.spawn_settings()
        elif key in ("pause", "resume"):
            self.set_paused(key == "pause")
        elif key in ("pill-show", "pill-hide"):
            self.set_overlay_visible(key == "pill-show")
        elif key == "quit":
            self.quit()

    def _pill_shown(self):
        ov = self.cfg["overlay"]
        return bool(ov.get("enabled", True)) and ov.get("idle", "pill") != "hidden"

    def _on_menu(self, item):
        item = item.lower().strip()
        if item == "settings":
            self.spawn_settings()
        elif item in ("pause", "pause kivi"):
            self.set_paused(True)
        elif item in ("resume", "resume kivi"):
            self.set_paused(False)
        elif item in ("toggle-pause", "pause/resume"):
            self.set_paused(not self.paused)
        elif item in ("hide", "hide pill"):
            self.set_overlay_visible(False)
        elif item in ("show", "show pill"):
            self.set_overlay_visible(True)
        elif item == "quit":
            self.quit()
        else:
            log.warning("unknown menu item %r", item)

    # ------------------------------------------------------------ hotkeys
    # The listener thread only queues work; _action_worker talks to the engine. A take is
    # identified by (engine run, listener session) so a release never ends a recording that
    # the hotkey didn't start (pill click, kivi toggle), and a take the engine ended on its own
    # (29 s limit, mic error, click) is dropped from the listener too.

    def _hotkeys_off(self):
        return self.paused or self.engine is None or time.monotonic() < self.capture_until

    def _on_start(self, action, session):
        if self._hotkeys_off():
            self.listener.cancel_session(session)
            return
        self._actions.put(("start", action, session, False))

    def _on_change(self, action, session):
        self._actions.put(("change", action, session, False))

    def _on_end(self, action, cancelled, session):
        self._actions.put(("end", action, session, cancelled))

    def _action_worker(self):
        while True:
            kind, action, session, cancelled = self._actions.get()
            try:
                self._do_action(kind, action, session, cancelled)
            except Exception as ex:
                log.error("hotkey action %s failed: %s", kind, ex)

    def _do_action(self, kind, action, session, cancelled):
        if kind == "start":
            run = self.engine.start(action)
            if run is None:  # engine busy or the mic failed: this take drives nothing
                if self.listener is not None:
                    self.listener.cancel_session(session)
                return
            with self._held_lock:
                self.held = (run, session)
            self._check_held()  # it may already be over (mic died instantly)
            return
        with self._held_lock:
            held = self.held
            if held is None or held[1] != session:
                return
            if kind == "end":
                self.held = None
        if kind == "change":
            self.engine.change(action, token=held[0])
        else:
            self.engine.end(action, cancelled, token=held[0])

    def _check_held(self):
        """Release the hotkey take if the engine is no longer recording it."""
        with self._held_lock:
            held = self.held
            if held is None:
                return
            if self.engine is not None and self.engine.state == "listening" and self.engine.run == held[0]:
                return
            self.held = None
        if self.listener is not None:
            self.listener.cancel_session(held[1])

    def _drop_held(self):
        with self._held_lock:
            held, self.held = self.held, None
        if held is not None:
            if self.listener is not None:
                self.listener.cancel_session(held[1])
            self.engine.end(None, True, token=held[0])

    # ------------------------------------------------------------ actions

    def set_paused(self, paused):
        self.paused = paused
        if paused:
            self._drop_held()
        log.info("hotkeys %s", "paused" if paused else "resumed")
        self._push_status()
        if self.tray is not None:
            self.tray.set_paused(paused)

    def set_capturing(self, active):
        """Settings is recording a new shortcut: keep the live hotkeys from firing meanwhile."""
        self.capture_until = time.monotonic() + CAPTURE_TIMEOUT if active else 0.0
        if active:
            self._drop_held()

    def set_overlay_visible(self, visible):
        """"hide pill" / "show pill" / kivi show|hide. Saved as overlay.idle, so the settings
        "when idle" option (or a restart) shows the same thing; active states always show."""
        from kivi import config

        patch = {"overlay": {"idle": "pill" if visible else "hidden"}}
        if visible:
            patch["overlay"]["enabled"] = True
        try:
            self.cfg = config.update_config(patch)
        except Exception as ex:  # read-only config: still apply it for this session
            log.error("could not save the pill setting: %s", ex)
            self.cfg = dict(self.cfg, overlay=dict(self.cfg["overlay"], **patch["overlay"]))
        if visible and self.overlay is None:
            self._ensure_overlay()
        if self.overlay is not None:
            self.overlay.reload(self._overlay_cfg())
        if self.tray is not None:
            self.tray.set_pill_visible(visible)
        return False

    def spawn_settings(self):
        cmd = [sys.executable, KIVI_BIN, "settings"]
        if shutil.which("systemd-run"):  # own scope, so restarting the service doesn't kill the window
            cmd = ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--", *cmd]
        env = {k: v for k, v in os.environ.items()
               if k not in ("LD_PRELOAD", PRELOAD_GUARD, "XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID")}
        token = self._activation_token()
        if token:  # lets KWin raise the window instead of opening it behind the active one
            env["XDG_ACTIVATION_TOKEN"] = env["DESKTOP_STARTUP_ID"] = token
        try:
            subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as ex:
            log.error("could not open settings: %s", ex)

    @staticmethod
    def _activation_token():
        try:
            from gi.repository import Gdk
            ctx = Gdk.Display.get_default().get_app_launch_context()
            return ctx.get_startup_notify_id(None, []) or None
        except Exception as ex:
            log.debug("no activation token: %s", ex)
            return None

    def reload(self):
        from kivi import config
        self.cfg = config.load_config()
        self.engine.reload(self.cfg)
        if self.listener is not None:
            try:
                self.listener.update(self.cfg["shortcuts"], min_hold=self.cfg["min_hold"],
                                     hands_free=self.cfg["hands_free"])
            except Exception as ex:
                self._ui("error", {"message": f"shortcut not saved: {ex}"})
        else:
            self._start_listener()
        self._ensure_overlay()
        if self.overlay is not None:
            self.overlay.reload(self._overlay_cfg())
        if self.tray is not None:
            self.tray.set_pill_visible(self._pill_shown())
        self._push_status()
        log.info("config reloaded")
        return False

    def _layer_shell(self):
        if self.overlay is not None and hasattr(self.overlay, "backend"):
            return self.overlay.backend == "layer-shell"
        from kivi import desktop
        return desktop.layer_shell_available() and LAYER_SHELL_LIB in os.environ.get("LD_PRELOAD", "")

    # ------------------------------------------------------------ ipc (server thread)

    def _ipc(self, msg):
        from gi.repository import GLib

        cmd = msg.get("cmd")
        if cmd == "status":
            return {"ok": True, "state": getattr(self.engine, "state", "idle"),
                    "devices": list(getattr(self.listener, "devices", []) or []),
                    "layer_shell": self._layer_shell(), "paused": self.paused}
        if cmd == "reload":
            GLib.idle_add(self.reload)
            return {"ok": True}
        if cmd == "toggle":
            mode = msg.get("mode", "dictate")
            if mode not in ("dictate", "command", "agent"):
                return {"ok": False, "error": f"unknown mode {mode!r}"}
            self.engine.toggle(mode)
            return {"ok": True, "state": self.engine.state}
        if cmd == "overlay":
            GLib.idle_add(self.set_overlay_visible, bool(msg.get("visible", True)))
            return {"ok": True}
        if cmd == "capture":
            self.set_capturing(bool(msg.get("active", False)))
            return {"ok": True}
        if cmd == "pause":
            GLib.idle_add(lambda: (self.set_paused(bool(msg.get("paused", True))), False)[1])
            return {"ok": True}
        if cmd == "quit":
            self.quit()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def main(argv):
    logging.basicConfig(level=logging.DEBUG if os.environ.get("KIVI_DEBUG") else logging.INFO,
                        format="kivi: %(message)s", stream=sys.stderr)
    _preload_layer_shell()

    from kivi import ipc
    if ipc.request({"cmd": "status"}) is not None:
        print("kivi is already running", file=sys.stderr)
        return 0

    import gi
    gi.require_version("Gtk", "4.0")
    return Daemon().run()
