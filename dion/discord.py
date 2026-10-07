from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
from typing import Optional

from .metadata.models import EpisodeItem, MediaItem

logger = logging.getLogger(__name__)

# Default Dion Application Client ID (can be configured in settings or via DION_DISCORD_CLIENT_ID)
DEFAULT_DISCORD_CLIENT_ID = "1557174111866851349"


class DiscordRPC:
    """Zero-dependency, non-blocking Discord Rich Presence client communicating via local IPC socket."""

    def __init__(self, client_id: Optional[str] = None):
        raw_id = (
            client_id
            or os.environ.get("DION_DISCORD_CLIENT_ID")
            or DEFAULT_DISCORD_CLIENT_ID
        )
        self.client_id = str(raw_id).strip() if raw_id else ""
        self._sock: Optional[socket.socket] = None
        self._pipe_file = None
        self._lock = threading.Lock()
        self._connected = False

    def _find_ipc_path(self) -> Optional[str]:
        """Locate active Discord IPC socket or named pipe on the host system."""
        if sys.platform == "win32":
            for i in range(10):
                pipe_path = rf"\\.\pipe\discord-ipc-{i}"
                if os.path.exists(pipe_path):
                    return pipe_path
            return None

        candidates = []
        for env_var in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
            val = os.environ.get(env_var)
            if val:
                candidates.append(val)
        candidates.append("/tmp")
        candidates.append(tempfile.gettempdir())

        for base in candidates:
            for i in range(10):
                sock_path = os.path.join(base, f"discord-ipc-{i}")
                if os.path.exists(sock_path):
                    return sock_path
        return None

    def connect(self) -> bool:
        """Establish local IPC handshake with Discord client."""
        with self._lock:
            if self._connected:
                return True
            if not self.client_id:
                return False

            path = self._find_ipc_path()
            if not path:
                return False

            try:
                if sys.platform == "win32":
                    self._pipe_file = open(path, "r+b", buffering=0)
                else:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(1.5)
                    sock.connect(path)
                    self._sock = sock

                # Send Handshake (Opcode 0)
                handshake_payload = json.dumps({"v": 1, "client_id": self.client_id}).encode("utf-8")
                self._send_frame(0, handshake_payload)

                # Receive Handshake response
                op, resp_data = self._read_frame()
                if op == 1:
                    self._connected = True
                    return True
                else:
                    self._close()
                    return False
            except Exception:
                self._close()
                return False

    def _send_frame(self, opcode: int, data: bytes) -> None:
        header = struct.pack("<II", opcode, len(data))
        if sys.platform == "win32" and self._pipe_file:
            self._pipe_file.write(header + data)
            self._pipe_file.flush()
        elif self._sock:
            self._sock.sendall(header + data)

    def _read_frame(self) -> tuple[Optional[int], Optional[bytes]]:
        if sys.platform == "win32" and self._pipe_file:
            header = self._pipe_file.read(8)
            if not header or len(header) < 8:
                return None, None
            opcode, length = struct.unpack("<II", header)
            data = self._pipe_file.read(length)
            return opcode, data
        elif self._sock:
            header = self._sock.recv(8)
            if not header or len(header) < 8:
                return None, None
            opcode, length = struct.unpack("<II", header)
            chunks = []
            bytes_left = length
            while bytes_left > 0:
                chunk = self._sock.recv(min(bytes_left, 4096))
                if not chunk:
                    break
                chunks.append(chunk)
                bytes_left -= len(chunk)
            return opcode, b"".join(chunks)
        return None, None

    def _send_activity(self, activity_data: Optional[dict]) -> None:
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {
                "pid": os.getpid(),
                "activity": activity_data,
            },
            "nonce": str(uuid.uuid4()),
        }
        encoded = json.dumps(payload).encode("utf-8")
        self._send_frame(1, encoded)
        # Drain response so socket buffer stays clear
        try:
            self._read_frame()
        except Exception:
            pass

    def start_playback(
        self,
        media: MediaItem,
        episode: Optional[EpisodeItem] = None,
        start_time: Optional[float] = None,
    ) -> None:
        """Asynchronously connect and display media playback on Discord."""
        def _worker():
            try:
                if not self.connect():
                    return

                if episode:
                    details = media.title
                    state = episode.display_name
                else:
                    details = f"{media.title} ({media.year})" if media.year else media.title
                    state = "Watching a movie"

                now = time.time()
                offset = start_time if (start_time and start_time > 10) else 0.0
                start_ts = int(now - offset)

                activity = {
                    "details": details[:128],
                    "state": state[:128],
                    "timestamps": {
                        "start": start_ts,
                    },
                }

                with self._lock:
                    if self._connected:
                        self._send_activity(activity)
            except Exception:
                pass

        threading.Thread(target=_worker, name="dion-discord-rpc", daemon=True).start()

    def stop(self) -> None:
        """Cleanly clear Discord presence and close IPC connection."""
        def _worker():
            with self._lock:
                if not self._connected:
                    return
                try:
                    self._send_activity(None)
                except Exception:
                    pass
                try:
                    self._send_frame(2, b"")
                except Exception:
                    pass
                self._close()

        t = threading.Thread(target=_worker, name="dion-discord-stop", daemon=True)
        t.start()
        t.join(timeout=0.8)

    def _close(self) -> None:
        self._connected = False
        if self._pipe_file:
            try:
                self._pipe_file.close()
            except Exception:
                pass
            self._pipe_file = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
