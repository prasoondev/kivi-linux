"""The kivi daemon: a windowless Gtk.Application wiring hotkeys, engine, overlay and IPC together."""

import logging
import os
import shutil
import signal
import subprocess
import sys

log = logging.getLogger("kivi")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIVI_BIN = os.path.join(ROOT, "bin", "kivi")
LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"
PRELOAD_GUARD = "KIVI_PRELOADED"
APP_ID = "ai.kivi.Linux.Daemon"

NO_INPUT_HINT = ("no keyboard access, so hotkeys are off. run `sudo usermod -aG input $USER` and log in again. "
                 "until then: click the pill, or bind `kivi toggle` to a shortcut in system settings.")


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
    """notify-send that replaces its previous bubble instead of stacking new ones."""

    def __init__(self):
        self.ok = bool(shutil.which("notify-send"))
        self.id = None

    def show(self, summary, body="", timeout=2500):
        if not self.ok:
            return
        cmd = ["notify-send", "-a", "kivi", "-i", "kivi", "-p", "-t", str(timeout),
               "-h", "string:x-kde-origin-name:kivi"]
        if self.id:
            cmd += ["-r", self.id]
        try:
            out = subprocess.run(cmd + ["--", summary, body], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return
        self.id = out.strip() or self.id


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
        self.engine = None
        self.listener = None
        self.server = None
        self.notifier = Notifier()
        self.paused = False
        self.held = None          # hotkey action currently driving the engine
        self.input_ok = False
        self.overlay_hidden = False

    def run(self):
        return self.app.run([sys.argv[0]])

    # ------------------------------------------------------------ lifecycle

    def _startup(self, app):
        from gi.repository import GLib

        from kivi import desktop, ipc
        from kivi.engine import Engine

        app.hold()
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal, sig)

        self._ensure_overlay()
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
            self.notifier.show("kivi", "stopped", 1)
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
        cfg = dict(self.cfg, overlay=dict(self.cfg["overlay"]))
        if self.overlay_hidden:
            cfg["overlay"]["idle"] = "hidden"
        return cfg

    def _overlay_live(self):
        return self.overlay is not None and self.cfg["overlay"].get("enabled", True)

    def _ui(self, event, data):
        """Engine → overlay (thread-safe) plus notification fallback. Called from any thread."""
        if event == "error":
            log.error("%s", data.get("message", ""))
        if self._overlay_live():
            try:
                self.overlay.handle(event, data)
            except Exception as ex:
                log.error("overlay: %s", ex)
        if not self._overlay_live() or not getattr(self.overlay, "available", True) or self.cfg.get("notify"):
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
            hint = "" if self.input_ok else NO_INPUT_HINT
            try:
                self.overlay.handle("status", {"paused": self.paused, "input": self.input_ok, "hint": hint})
            except Exception as ex:
                log.debug("overlay status: %s", ex)

    def _on_click(self):
        self.engine.toggle("agent")

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

    # ------------------------------------------------------------ hotkeys (listener thread)

    def _on_start(self, action):
        if self.paused or self.engine is None:
            return
        self.held = action
        self.engine.start(action)

    def _on_change(self, action):
        if self.held is not None:
            self.held = action
            self.engine.change(action)

    def _on_end(self, action, cancelled):
        if self.held is None:
            return
        self.held = None
        self.engine.end(action, cancelled)

    # ------------------------------------------------------------ actions

    def set_paused(self, paused):
        self.paused = paused
        held, self.held = self.held, None
        if paused and held is not None:
            self.engine.end(held, True)
        log.info("hotkeys %s", "paused" if paused else "resumed")
        self._push_status()

    def set_overlay_visible(self, visible):
        """Runtime only: a hidden pill still appears while kivi is listening or replying."""
        self.overlay_hidden = not visible
        if visible and self.overlay is None:
            self.cfg = dict(self.cfg, overlay=dict(self.cfg["overlay"], enabled=True))
            self._ensure_overlay()
        if self.overlay is not None:
            self.overlay.reload(self._overlay_cfg())

    def spawn_settings(self):
        cmd = [sys.executable, KIVI_BIN, "settings"]
        if shutil.which("systemd-run"):  # own scope, so restarting the service doesn't kill the window
            cmd = ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--", *cmd]
        env = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", PRELOAD_GUARD)}
        try:
            subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as ex:
            log.error("could not open settings: %s", ex)

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
