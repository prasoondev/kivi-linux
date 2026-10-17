"""Newline-delimited JSON over a unix socket between the daemon and the CLI / settings app."""

import json
import os
import socket
import threading
from typing import Callable

from . import config


class Server:
    """Accepts connections in a background thread and answers each JSON line with handler(msg)."""

    def __init__(self, handler: Callable[[dict], dict], path: str | None = None):
        self.handler = handler
        self.path = path or config.SOCKET_PATH
        self._sock = None
        self._thread = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if os.path.exists(self.path):
            if request({"cmd": "status"}, timeout=0.5, path=self.path) is not None:
                raise RuntimeError(f"another kivi daemon is already listening on {self.path}")
            os.unlink(self.path)  # stale socket from a crashed daemon
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            sock.bind(self.path)
        finally:
            os.umask(old_umask)
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock
        self._stopping.clear()
        self._thread = threading.Thread(target=self._serve, name="kivi-ipc", daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stopping.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        with conn:
            conn.settimeout(10)
            try:
                for line in conn.makefile("r", encoding="utf-8"):
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        msg = None
                    if not isinstance(msg, dict):
                        reply = {"ok": False, "error": "expected a json object"}
                    else:
                        try:
                            reply = self.handler(msg)
                        except Exception as e:
                            reply = {"ok": False, "error": str(e)}
                    conn.sendall((json.dumps(reply if reply is not None else {"ok": True}) + "\n").encode())
            except OSError:
                pass

    def stop(self) -> None:
        self._stopping.set()
        if self._sock:
            self._sock.close()
            self._sock = None
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def request(msg: dict, timeout=2.0, path: str | None = None) -> dict | None:
    """Send one request to the daemon; None if it isn't running or doesn't answer."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path or config.SOCKET_PATH)
            s.sendall((json.dumps(msg) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf) if buf.strip() else None
    except (OSError, ValueError):
        return None
