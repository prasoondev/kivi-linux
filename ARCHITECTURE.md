# kivi-linux architecture (v2)

A Linux (KDE Plasma 6, Wayland; must also degrade gracefully on X11/GNOME) clone of heykivi.ai,
powered by Sarvam AI. Python 3.14 **system python only** — no pip. Available system modules:
`evdev` (python-evdev 2.0), `gi` with Gtk 4.24 + Adw 1.10, stdlib. Optional at runtime:
`gtk4-layer-shell` (typelib `Gtk4LayerShell-1.0`, lib `/usr/lib/libgtk4-layer-shell.so`) — NOT
installed yet, code must work without it. Tools present: pw-record, pw-play, notify-send, kdialog,
qdbus6, gdbus. Maybe absent: wl-copy/wl-paste.

The v1 single file `kivi.py` is working and tested; port its logic (prompts, multipart upload,
clipboard fallbacks, uinput combo, cancel-on-other-key, min_hold) into the package below, then
delete `kivi.py` (the entry point becomes `bin/kivi`).

## Verified Sarvam API facts (do not change)
- STT: `POST https://api.sarvam.ai/speech-to-text`, multipart, header `api-subscription-key`.
  fields `file`, `model=saaras:v4`, `language_code=unknown|hi-IN|en-IN|…`, optional `keyterms`
  (JSON array ≤50 strings ≤64 chars, saaras:v4 only). Response `{"transcript", "language_code"}`.
  Sync limit ≈30 s of audio. 16 kHz mono s16 WAV works. Latency ~0.6–1.5 s.
- Chat: `POST https://api.sarvam.ai/v1/chat/completions`, OpenAI-shaped, headers
  `api-subscription-key` + `Authorization: Bearer <key>`. Models `sarvam-105b`,
  `sarvam-105b-conversations`. **Always send `"reasoning_effort": null`** (otherwise ~6 s of
  hidden reasoning). Tools: OpenAI function-tool format works with `sarvam-105b-conversations`
  (returns `message.tool_calls[].function.{name,arguments(json string)}`, finish_reason `tool_calls`).
  Content may start with "\n" — strip. Strip `<think>…</think>` defensively.
- TTS: `POST https://api.sarvam.ai/text-to-speech` JSON `{"text", "target_language_code":"en-IN"|"hi-IN"|…,
  "model":"bulbul:v3", "speaker":"shubh"}` → `{"audios":[base64 WAV]}`. Play with `pw-play file.wav`.
  Check https://docs.sarvam.ai for the bulbul:v3 speaker list for the settings dropdown.

## Files & paths
- `~/.config/kivi/config.toml` (settings), `~/.config/kivi/api_key` (chmod 600, already holds the
  user's key — never print it, never log it, never commit it), `~/.config/kivi/dictionary.txt`
  (one learned word per line), `~/.local/share/kivi/history.jsonl` (one JSON object per dictation).
- IPC socket: `$XDG_RUNTIME_DIR/kivi.sock` (fallback `/tmp/kivi-$UID.sock`).

## Package layout
```
kivi-linux/
  bin/kivi                 # CLI entry (python3 shebang, adds repo root to sys.path)
  kivi/__init__.py         # __version__ = "0.2.0"
  kivi/config.py           # config/key/dictionary/history I/O
  kivi/sarvam.py           # API client
  kivi/audio.py            # recorder with live levels, wav player
  kivi/desktop.py          # clipboard, uinput keyboard, notify, launching, window helpers
  kivi/hotkeys.py          # evdev hold-combo listener
  kivi/engine.py           # state machine: dictate / command / agent
  kivi/agent.py            # voice agent: tools + conversation memory
  kivi/ipc.py              # unix-socket server (daemon) + client (CLI/settings)
  kivi/theme.py            # shared CSS (kivi look) for overlay + settings
  kivi/overlay.py          # always-on desktop pill (GTK4, layer-shell or KWin fallback)
  kivi/daemon.py           # Gtk.Application wiring hotkeys+engine+overlay+ipc
  kivi/settings.py         # Adw manager app ("kivi settings")
  data/kivi.svg            # original icon (NOT Kivi's logo): dark rounded square #0b0e0a with 5 green #a8e063 bars
  data/kivi.desktop        # launches `kivi settings`
  kivi.service             # systemd user unit: ExecStart=%h/.local/bin/kivi daemon
  install.sh  README.md  config.example.toml
```

## Interfaces (contracts between modules — implement exactly)

### config.py
```python
CONFIG_DIR, CONFIG_PATH, KEY_PATH, WORDS_PATH, HISTORY_PATH, SOCKET_PATH  # str constants
DEFAULTS: dict  # see schema below
def load_config() -> dict          # DEFAULTS deep-merged with file; adds cfg["api_key"] from env SARVAM_API_KEY > file; "" if none (never sys.exit)
def save_config(cfg: dict) -> None # writes TOML (own tiny dumper: str/bool/int/float/list[str]/one level of tables); never writes api_key; atomic (tmp+rename)
def get_api_key() -> str ; def set_api_key(key: str) -> None   # chmod 600, dir 700
def load_words() -> list[str] ; def add_word(w) ; def remove_word(w) ; def save_words(list)
def append_history(entry: dict) -> None   # adds "ts" (epoch float); trims file to last 500 entries occasionally
def load_history(limit=200) -> list[dict] # newest first
def clear_history() -> None
```
Config schema / DEFAULTS:
```toml
language = "unknown"
stt_model = "saaras:v4"
cleanup = true
cleanup_model = "sarvam-105b-conversations"
command_model = "sarvam-105b"
agent_model = "sarvam-105b-conversations"
style = ""                 # global persona
paste_keys = "ctrl+v"
restore_clipboard = true
min_hold = 0.35
hands_free = true          # double-tap dictate shortcut => latched recording until next tap
history = true
notify = false             # notifications are a fallback only when overlay unavailable
agent_speak = true         # agent replies are spoken with TTS
tts_speaker = "shubh"
tts_language = "en-IN"
agent_allow_shell = false  # run_command tool only if true
[shortcuts]                # hold-combos of evdev names without KEY_ prefix, joined by "+"
dictate = "RIGHTCTRL"
command = "RIGHTCTRL+RIGHTSHIFT"
agent = "RIGHTALT"
[overlay]
enabled = true
position = "bottom"        # "bottom" | "top"
idle = "pill"              # "pill" (always visible small pill) | "hidden"
margin = 28
```
History entry: `{"mode": "dictate"|"command"|"agent", "heard": str, "result": str, "language": str|None, "ms": int}`.

### sarvam.py
```python
class SarvamError(RuntimeError)
def transcribe(api_key, wav_path, *, model="saaras:v4", language="unknown", keyterms=()) -> tuple[str, str|None]  # (transcript, language_code)
def chat(api_key, model, messages, *, tools=None, temperature=0.2, max_tokens=1024) -> dict  # returns choices[0]["message"] dict (content stripped)
def complete(api_key, model, system, user) -> str      # convenience: text only
def tts(api_key, text, *, language="en-IN", speaker="shubh", model="bulbul:v3") -> bytes  # WAV bytes; text chunked ≤ 500 chars if needed
def check_key(api_key) -> tuple[bool, str]   # cheap validation (tiny chat call); (ok, message)
```
stdlib urllib only; timeouts 30 s; errors raise SarvamError with readable message (status + server message).

### audio.py
```python
class Recorder:
    def __init__(self, on_level: Callable[[float], None] | None = None)  # level 0..1, ~30 Hz, called from reader thread
    def start(self) -> None     # spawns `pw-record --rate 16000 --channels 1 --format s16 --raw -` (verify flag; else arecord -t raw) reading PCM from stdout
    def stop(self) -> str | None  # returns path to a WAV (wave module) or None if nothing recorded; also trims leading/trailing silence lightly is optional
    def cancel(self) -> None
    @property duration -> float
    max_seconds = 29.0          # auto-stop guard: calls on_autostop callback attribute if set
def play_wav_bytes(data: bytes) -> subprocess.Popen   # temp file + pw-play (fallback aplay); deletes after
def stop_playback() -> None
```
Level = clamp(rms/3000, 0, 1) smoothed, so the overlay bars dance with the voice.

### desktop.py
```python
class Clipboard: get()->str ; set(text)        # wl-clipboard > qdbus6 klipper > xclip (port from v1)
class Keyboard: combo("ctrl+v")                # python-evdev UInput named "kivi-virtual-keyboard"; create once
def insert_text(kbd, clip, text, paste_keys="ctrl+v", restore=True) -> None   # v1 insert logic
def selected_text(kbd, clip) -> str            # sentinel + ctrl+c logic from v1 ("" if none)
def notify(summary, body="", timeout_ms=2500) -> None
def open_url(url) ; def launch_app(name) -> bool   # xdg-open; launch: search .desktop files in XDG_DATA_DIRS/applications by Name/exec (case-insensitive fuzzy), run via `gtk-launch <id>` or `kioclient exec`
def input_access_ok() -> bool                  # can open at least one /dev/input/event* keyboard
def uinput_ok() -> bool
def layer_shell_available() -> bool            # typelib Gtk4LayerShell-1.0 + /usr/lib/libgtk4-layer-shell.so exist
```

### hotkeys.py
```python
def parse_combo(s: str) -> frozenset[int]          # "RIGHTCTRL+RIGHTSHIFT" -> evdev codes; accepts with/without KEY_ prefix; raises ValueError
def combo_label(s: str) -> str                     # "right ctrl + right shift" (lowercase, kivi voice)
class HotkeyListener(threading.Thread):
    def __init__(self, shortcuts: dict[str, str], on_start, on_change, on_end, *, min_hold=0.35, hands_free=True)
        # on_start(action) when a combo becomes fully held (longest matching combo wins);
        # on_change(action) when held keys upgrade to a longer combo (e.g. dictate -> command);
        # on_end(action, cancelled: bool) when any combo key is released (or hands-free latch tapped again).
        # cancelled=True if held < min_hold, or a non-combo key was pressed during the hold (it was a normal shortcut).
        # hands-free: two taps (< min_hold each) of the dictate combo within 0.4 s => on_start("dictate") and latch until next tap -> on_end.
    def update(self, shortcuts, *, min_hold=None, hands_free=None)  # live reload
    def stop(self)
    devices: list[str]   # names of keyboards being read; hot-plug: rescan every 3 s
```
Callbacks are invoked from the listener thread. Ignore the uinput device named "kivi-virtual-keyboard". Ignore autorepeat (value 2).

### agent.py
```python
TOOLS: list[dict]   # OpenAI function schemas: type_text(text), open_url(url), launch_app(name), copy_to_clipboard(text),
                    # get_selected_text(), run_command(command) [only offered if cfg agent_allow_shell], remember_word(word)
class Agent:
    def __init__(self, cfg, desktop_ctx)   # desktop_ctx gives kbd, clip
    def handle(self, utterance: str) -> str   # loop: chat with tools (max 4 rounds), execute tool calls, return final short reply text
    def reset(self)                            # memory resets automatically after 5 min idle; keeps last 10 turns
```
System prompt: lowercase-friendly concise assistant "kivi", living on the user's Linux desktop, replies in ≤2 sentences
in the user's language (Hinglish OK), uses tools to act; current date/time and desktop (KDE Plasma) in prompt.
`run_command`: execute with `bash -lc`, 15 s timeout, return truncated output (2 KB).

### engine.py
```python
class Engine:
    def __init__(self, cfg: dict, ui: Callable[[str, dict], None])
        # ui(event, data) — events below; called from worker threads (overlay marshals to GTK thread)
    def start(self, mode)          # mode in {"dictate","command","agent"}: begin recording (if not busy)
    def change(self, mode)         # upgrade mode while recording
    def end(self, mode, cancelled) # stop recording; if not cancelled process in a worker thread
    def toggle(self, mode)         # for overlay click / IPC: start if idle, end if recording
    def reload(self, cfg)
    state: str                     # "idle" | "listening" | "thinking" | "speaking"
```
UI events: `("listening", {"mode"})`, `("level", {"value": float})`, `("thinking", {"mode"})`,
`("done", {"mode", "text"})` (dictation inserted / edit done), `("reply", {"text", "heard"})` (agent/ask answer,
overlay shows a card), `("speaking", {})`, `("idle", {})`, `("error", {"message"})`, `("cancelled", {})`,
`("heard", {"text"})` (raw transcript, shown briefly as "you said …").
Modes: dictate = STT → cleanup → insert. command = if selection: rewrite+replace, else ask → reply.
agent = STT → Agent.handle → reply (+ TTS if agent_speak; "speaking" while playing; a new start() stops playback).
History appended for every completed run if cfg history.

### ipc.py
Newline-delimited JSON over the unix socket. Requests: `{"cmd":"status"}` → `{"ok":true,"state":…,"devices":[…],"layer_shell":bool}`;
`{"cmd":"reload"}` (daemon re-reads config + hotkeys); `{"cmd":"toggle","mode":"dictate|agent|command"}`;
`{"cmd":"overlay","visible":bool}`; `{"cmd":"quit"}`.
```python
class Server: __init__(handler: Callable[[dict], dict]); start(); stop()   # thread; removes stale socket
def request(msg: dict, timeout=2.0) -> dict | None    # None if daemon not running
```

### theme.py — the kivi look (taken from heykivi.ai's CSS)
Tokens: stage/dusk-0 `#0b0e0a`, dusk-1 `#121710`, dusk-2 `#1a2016`, ink `#f4f1e6`, ink-2 `#b9b6a8`, ink-3 `#7d7b6f`,
hairline `rgba(255,255,255,0.08)` / strong `0.12`, paper `#f6f4ec`, green-glow `#a8e063`, green-deep `#256b2f`,
green-tint `rgba(168,224,99,0.18)`, amber `#c77e28`, error `#ff6b5b`. Radii 4/8/12/20/pill 999.
Fonts (installed in ~/.local/share/fonts): display **"Fraunces"** (serif, use for headings / big text, weight 400–500),
body **"Switzer Variable"**, fallbacks system-ui. Voice: everything lowercase ("you talk. kivi types.", "you said",
"kivi remembered", "listening…", "thinking…"). Motion: ease-enter cubic-bezier(0.16,1,0.3,1) 250 ms.
Signature listening glyph: 5 thin rounded green bars (2 px wide, 13 px tall, rest scales .42 .78 .56 .9 .64)
that animate with mic level. Wordmark: lowercase "kivi" set in Fraunces, cream.
`OVERLAY_CSS: str`, `SETTINGS_CSS: str`, `def apply(css: str)` (Gtk.CssProvider on default display, priority USER).

### overlay.py
```python
class Overlay:
    def __init__(self, app: Gtk.Application, cfg, on_click: Callable[[], None], on_menu: Callable[[str], None])
    def handle(self, event: str, data: dict)   # thread-safe (GLib.idle_add internally)
    def reload(self, cfg)
```
- Always-on pill, bottom-center (or top) of the focused monitor. Idle: small dark pill (~44×14 px look, 5 dim bars,
  translucent dusk-1 with hairline border, subtle shadow). Hover idle pill → shows hint "hold right ctrl to talk · click for agent".
  Listening: expands (animated width) to ~180×44 showing green bars driven by `level` events (+ mode label: "listening",
  "editing", "kivi agent"). Thinking: bars become a slow wave/shimmer, label "thinking…". Heard: "you said …" line in ink-2.
  Done: green check + first ~40 chars, collapse after 1.2 s. Reply: card (max 420 px wide) above the pill with the answer
  in Fraunces 15 px, "you asked …" in small ink-3, auto-dismiss after max(6 s, text length based), click to dismiss, copy button.
  Error: amber/red text 3 s.
- Left click on pill = engine toggle("agent") (click to talk, click again to send). Right click = popover menu:
  "settings", "pause kivi"/"resume kivi", "hide pill", "quit".
- Window placement: if layer-shell available: `Gtk4LayerShell.init_for_window`, layer OVERLAY, anchor bottom (or top),
  margin from cfg, keyboard mode NONE, no exclusive zone. Requires the library to be preloaded BEFORE Gtk is imported —
  daemon.py handles `LD_PRELOAD` re-exec. Fallback (no layer-shell, KDE): undecorated transparent window titled
  "kivi-overlay"; then load a KWin script over D-Bus (`org.kde.KWin /Scripting loadScript(path, name)` then
  `/Scripting/Script<N> run`, or `org.kde.kwin.Scripting`) that finds the window by caption and sets keepAbove, skipTaskbar,
  skipPager, skipSwitcher, onAllDesktops, noBorder and moves it to bottom-center of its output; also make the window
  input-transparent except the pill area if possible. Any other env: plain undecorated window + notifications fallback.
  The overlay must never steal keyboard focus (focus would break pasting into the user's app).
- Transparent background: window CSS `background: transparent`; only the pill draws.

### daemon.py
`def main(argv)`: if layer-shell lib exists and `LD_PRELOAD` lacks it → `os.execve` self with it prepended (set a guard env
var to avoid loops). Create `Adw.Application`/`Gtk.Application(application_id="ai.kivi.Linux.Daemon")` hold()s itself (no main
window). Wires: Engine(ui=overlay.handle + notify fallback), HotkeyListener(callbacks → engine), ipc.Server, Overlay.
If no input access: still runs (overlay click + IPC toggle work); log a clear message and show it in the pill tooltip.
"pause" disables hotkeys. "settings" spawns `kivi settings`. SIGTERM/SIGINT quit cleanly.

### settings.py — "kivi" manager app (Adw)
`Adw.Application(application_id="ai.kivi.Linux")`, window ~900×640, dark-forced kivi theme (Adw.StyleManager FORCE_DARK + SETTINGS_CSS).
Left sidebar (Adw.NavigationSplitView) with lowercase pages:
1. **home** — big Fraunces "you talk. kivi types." hero, status card (daemon running? via ipc status; start/stop/restart via
   `systemctl --user`), "launch at login" switch (`systemctl --user enable/disable kivi`), checklist with fix-it hints:
   api key set ✓, keyboard access (input group) ✓/✗ with the exact command to copy, layer-shell (optional, nicer pill) ✓/✗
   with the pacman command to copy, wl-clipboard optional. A "try it" text area where the user can focus and dictate.
2. **api key** — replaces sign-in: password entry (Adw.PasswordEntryRow) prefilled masked, "save" + "test key"
   (sarvam.check_key in a thread, spinner, result), link to dashboard.sarvam.ai.
3. **shortcuts** — rows for dictate / edit & command / voice agent; each shows the combo as keycaps
   (rounded boxes, mono font, like Kivi's keycaps `#efeade` ink on dark); "change" button opens a capture dialog: "press and
   hold your new shortcut…", capture via Gtk.EventControllerKey on the dialog: evdev code = hardware keycode − 8, name via
   `evdev.ecodes.KEY[code]` stripped of "KEY_"; combo = all keys held at the moment of the first release; Esc cancels;
   reset-to-default button. Also: hands-free switch, min hold slider. Explain modes in kivi voice
   ("hold to talk. let go, kivi types.").
4. **voice & language** — language dropdown (auto + the 23 codes with human names), cleanup switch, agent voice dropdown
   (bulbul:v3 speakers) + "preview voice" button (tts + play), speak replies switch, tts language.
5. **style** — persona TextView for `style` with three example chips (developer / messaging / productivity copied from
   Kivi's personas, e.g. dev: "technical and terse. keep code identifiers in backticks.").
6. **memory** — "kivi remembered" list of dictionary words with remove buttons + add entry.
7. **history** — list of past runs: "you said" (ink-2) → result (ink), mode tag, time ago; copy button; clear all.
8. **agent** — explain voice agent, allow shell commands switch (with warning), pill settings (enabled, position, idle style).
Every change: `save_config` then `ipc.request({"cmd":"reload"})`. All network calls off the GTK thread.

### bin/kivi CLI
`kivi daemon` (default when no args) · `kivi settings` · `kivi toggle [dictate|agent|command]` (via IPC; for binding in
KDE shortcuts if no input access) · `kivi add-word WORD` · `kivi words` · `kivi test FILE.wav` · `kivi status`.

### install.sh
Idempotent, no sudo inside. Symlink bin/kivi → ~/.local/bin/kivi; install icon to ~/.local/share/icons/hicolor/scalable/apps/kivi.svg;
desktop file to ~/.local/share/applications/ai.kivi.Linux.desktop; copy config.example.toml if no config; install + enable +
(re)start systemd user unit; print the one-line sudo command for anything missing:
`sudo pacman -S --needed gtk4-layer-shell wl-clipboard && sudo usermod -aG input $USER` plus an immediate no-logout
workaround `sudo setfacl -m u:$USER:rw /dev/input/event*` (until reboot).
