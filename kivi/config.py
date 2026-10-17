"""Settings, API key, learned words and dictation history on disk.

Paths can be redirected for tests with KIVI_CONFIG_DIR / KIVI_DATA_DIR.
"""

import copy
import json
import os
import tempfile
import time
import tomllib

CONFIG_DIR = os.environ.get("KIVI_CONFIG_DIR") or os.path.expanduser("~/.config/kivi")
DATA_DIR = os.environ.get("KIVI_DATA_DIR") or os.path.expanduser("~/.local/share/kivi")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.toml")
KEY_PATH = os.path.join(CONFIG_DIR, "api_key")
WORDS_PATH = os.path.join(CONFIG_DIR, "dictionary.txt")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
SOCKET_PATH = (os.path.join(os.environ["XDG_RUNTIME_DIR"], "kivi.sock")
               if os.environ.get("XDG_RUNTIME_DIR") else f"/tmp/kivi-{os.getuid()}.sock")

DEFAULTS = {
    "language": "unknown",
    "stt_model": "saaras:v4",
    "cleanup": True,
    "cleanup_model": "sarvam-105b-conversations",
    "command_model": "sarvam-105b",
    "agent_model": "sarvam-105b-conversations",
    "style": "",
    "paste_keys": "ctrl+v",
    "restore_clipboard": True,
    "min_hold": 0.35,
    "hands_free": True,
    "history": True,
    "notify": False,
    "agent_speak": True,
    "tts_speaker": "shubh",
    "tts_language": "en-IN",
    "agent_allow_shell": False,
    "shortcuts": {
        "dictate": "RIGHTCTRL",
        "command": "RIGHTCTRL+RIGHTSHIFT",
        "agent": "RIGHTALT",
    },
    "overlay": {
        "enabled": True,
        "position": "bottom",
        "idle": "pill",
        "margin": 28,
    },
}

HISTORY_KEEP = 500


def _atomic_write(path, data, mode=0o644):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".kivi-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate_v1(data):
    """Map v1's hotkey/command_key onto [shortcuts]."""
    hotkey = data.pop("hotkey", None)
    command_key = data.pop("command_key", None)
    if hotkey and "shortcuts" not in data:
        hotkey = hotkey.removeprefix("KEY_")
        data["shortcuts"] = {"dictate": hotkey}
        if command_key:
            data["shortcuts"]["command"] = f"{hotkey}+{command_key.removeprefix('KEY_')}"
    return data


def load_config() -> dict:
    data = {}
    try:
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"kivi: ignoring unreadable {CONFIG_PATH}: {e}")
    data.pop("api_key", None)
    cfg = _merge(DEFAULTS, _migrate_v1(data))
    cfg["api_key"] = get_api_key()
    return cfg


# ---------------------------------------------------------------- tiny TOML writer

def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _toml_key(k):
    return k if k.replace("_", "").replace("-", "").isalnum() and k.isascii() else json.dumps(k)


def dumps_toml(cfg: dict) -> str:
    lines, tables = [], []
    for k, v in cfg.items():
        if k == "api_key" or v is None:
            continue
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    for name, table in tables:
        lines += ["", f"[{_toml_key(name)}]"]
        lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table.items() if v is not None]
    return "\n".join(lines) + "\n"


def save_config(cfg: dict) -> None:
    _atomic_write(CONFIG_PATH, "# kivi settings (written by kivi; api key lives in api_key)\n" + dumps_toml(cfg))


# ---------------------------------------------------------------- api key

def get_api_key() -> str:
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(KEY_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def set_api_key(key: str) -> None:
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    _atomic_write(KEY_PATH, key.strip() + "\n", mode=0o600)


# ---------------------------------------------------------------- dictionary

def load_words() -> list[str]:
    try:
        with open(WORDS_PATH, encoding="utf-8") as f:
            words = [w.strip() for w in f]
    except OSError:
        return []
    return list(dict.fromkeys(w for w in words if w))


def save_words(words) -> None:
    clean = list(dict.fromkeys(w.strip() for w in words if w and w.strip()))
    _atomic_write(WORDS_PATH, "".join(w + "\n" for w in clean))


def add_word(w) -> None:
    words = load_words()
    w = w.strip()
    if w and w not in words:
        save_words(words + [w])


def remove_word(w) -> None:
    words = load_words()
    if w.strip() in words:
        save_words([x for x in words if x != w.strip()])


# ---------------------------------------------------------------- history

def append_history(entry: dict) -> None:
    entry = {**entry, "ts": time.time()}
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    fd = os.open(HISTORY_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with open(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Trim occasionally: only once the file is clearly past the cap.
    try:
        if os.path.getsize(HISTORY_PATH) > 256 * 1024:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > HISTORY_KEEP:
                _atomic_write(HISTORY_PATH, "".join(lines[-HISTORY_KEEP:]), mode=0o600)
    except OSError:
        pass


def load_history(limit=200) -> list[dict]:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if limit and len(out) >= limit:
            break
    return out


def clear_history() -> None:
    try:
        os.unlink(HISTORY_PATH)
    except FileNotFoundError:
        pass
