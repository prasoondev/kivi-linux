"""Microphone recording with live levels (pw-record / arecord raw PCM) and WAV playback."""

import array
import math
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from typing import Callable

RATE = 16000
BYTES_PER_SEC = RATE * 2  # mono s16
CHUNK = BYTES_PER_SEC // 30  # ~30 level updates per second
MIN_SECONDS = 0.15


def _record_cmd():
    if shutil.which("pw-record"):
        return ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16",
                "--latency", "20ms", "--raw", "-"]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-"]
    raise RuntimeError("no recorder found: install pipewire (pw-record) or alsa-utils (arecord)")


class _Meter:
    """Turns PCM chunks into a 0..1 voice level.

    Some mics deliver a large DC offset and a hiss floor, so the mean is removed and an
    adaptive noise floor is subtracted before scaling by 3000.
    """

    def __init__(self):
        self.floor = None
        self.level = 0.0

    def feed(self, chunk: bytes) -> float:
        samples = array.array("h", chunk[: len(chunk) // 2 * 2])
        if not samples:
            return self.level
        mean = sum(samples) / len(samples)
        rms = math.sqrt(sum((s - mean) ** 2 for s in samples) / len(samples))
        if self.floor is None or rms < self.floor:
            self.floor = rms
        else:
            self.floor += (rms - self.floor) * 0.005
        raw = min(max((rms - self.floor * 1.3) / 3000.0, 0.0), 1.0)
        self.level += (raw - self.level) * (0.6 if raw > self.level else 0.25)
        return self.level


class Recorder:
    max_seconds = 29.0

    def __init__(self, on_level: Callable[[float], None] | None = None):
        self.on_level = on_level
        self.on_autostop: Callable[[], None] | None = None
        self._proc = None
        self._thread = None
        self._pcm = bytearray()
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._proc is not None

    @property
    def duration(self) -> float:
        return len(self._pcm) / BYTES_PER_SEC

    def start(self) -> None:
        if self._proc:
            self.cancel()
        self._pcm = bytearray()
        self._proc = subprocess.Popen(_record_cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      stdin=subprocess.DEVNULL)
        self._thread = threading.Thread(target=self._read, args=(self._proc,), name="kivi-recorder", daemon=True)
        self._thread.start()

    def _read(self, proc):
        meter = _Meter()
        limit = int(self.max_seconds * BYTES_PER_SEC)
        pending = b""
        autostopped = False
        while True:
            data = proc.stdout.read1(CHUNK)
            if not data:
                break
            with self._lock:
                room = limit - len(self._pcm)
                if room > 0:
                    self._pcm += data[:room]
            pending += data
            while len(pending) >= CHUNK:
                level = meter.feed(pending[:CHUNK])
                pending = pending[CHUNK:]
                if self.on_level:
                    try:
                        self.on_level(level)
                    except Exception:
                        pass
            if room <= len(data) and not autostopped:
                autostopped = True
                if self.on_autostop:
                    threading.Thread(target=self.on_autostop, daemon=True).start()

    def _halt(self):
        proc, self._proc = self._proc, None
        if not proc:
            return False
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        proc.stdout.close()
        return True

    def stop(self) -> str | None:
        if not self._halt():
            return None
        with self._lock:
            pcm = bytes(self._pcm)
        if len(pcm) < MIN_SECONDS * BYTES_PER_SEC:
            return None
        fd, path = tempfile.mkstemp(prefix="kivi-", suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(_remove_dc(pcm))
        return path

    def cancel(self) -> None:
        self._halt()
        self._pcm = bytearray()


def _remove_dc(pcm: bytes) -> bytes:
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    if not samples:
        return b""
    mean = round(sum(samples) / len(samples))
    if abs(mean) < 64:
        return samples.tobytes()
    return array.array("h", [min(32767, max(-32768, s - mean)) for s in samples]).tobytes()


# ---------------------------------------------------------------- playback

_players: set[subprocess.Popen] = set()
_players_lock = threading.Lock()


def play_wav_bytes(data: bytes) -> subprocess.Popen:
    fd, path = tempfile.mkstemp(prefix="kivi-say-", suffix=".wav")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    cmd = ["pw-play", path] if shutil.which("pw-play") else ["aplay", "-q", path]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        os.unlink(path)
        raise
    with _players_lock:
        _players.add(proc)

    def reap():
        proc.wait()
        with _players_lock:
            _players.discard(proc)
        try:
            os.unlink(path)
        except OSError:
            pass

    threading.Thread(target=reap, name="kivi-player", daemon=True).start()
    return proc


def stop_playback() -> None:
    with _players_lock:
        procs = list(_players)
    for p in procs:
        if p.poll() is None:
            p.terminate()


if __name__ == "__main__":
    import sys
    import time

    levels = []
    r = Recorder(on_level=levels.append)
    r.start()
    time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 1.0)
    print(f"duration {r.duration:.2f}s")
    path = r.stop()
    print("wav", path, os.path.getsize(path) if path else 0, "bytes")
    print(f"{len(levels)} levels, max {max(levels, default=0):.2f}:", " ".join(f"{x:.2f}" for x in levels[::3]))
    if path:
        os.unlink(path)
