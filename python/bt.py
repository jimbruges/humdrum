"""Bluetooth: talks to the host helper (host/bluetooth/humdrum-bt).

The app runs in a container without access to the system's Bluetooth stack,
so pairing and media controls (AVRCP) are done by a small service on the
board. It listens on a Unix socket in the app's data folder, which the
container shares: one JSON request per line, one JSON reply per line.

The audio itself arrives through PipeWire; `audio_node()` finds the phone's
stream so the engine can record it like a second microphone.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from pathlib import Path

log = logging.getLogger("humdrum.bt")

POLL_S = 0.5
EMPTY = {"available": False, "powered": False, "discoverable": False, "connected": False,
         "device": "", "playing": False, "title": "", "artist": "", "error": ""}


class Bluetooth:
    def __init__(self, data_dir: Path) -> None:
        self.sock_path = data_dir / "bt.sock"
        self.state: dict = dict(EMPTY)
        self.on_change = None  # callback(state), from the poll thread
        self._lock = threading.Lock()
        threading.Thread(target=self._poll, name="bt", daemon=True).start()

    def request(self, cmd: str, **args) -> dict:
        """Send one command to the helper; {"error": ...} if it isn't running."""
        with self._lock:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(3)
                    s.connect(str(self.sock_path))
                    s.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
                    buf = b""
                    while not buf.endswith(b"\n"):
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                return json.loads(buf or b"{}")
            except (OSError, ValueError) as e:
                return {"error": f"Bluetooth helper not running ({e.__class__.__name__})"}

    def command(self, cmd: str, **args) -> dict:
        reply = self.request(cmd, **args)
        if "error" in reply:
            log.warning("bluetooth %s: %s", cmd, reply["error"])
        self._refresh()
        return reply

    def _refresh(self) -> None:
        reply = self.request("status")
        new = dict(EMPTY)
        if "error" in reply:
            new["error"] = reply["error"]
        else:
            new.update({k: reply.get(k, v) for k, v in EMPTY.items()})
            new["available"] = True
        if new != self.state:
            self.state = new
            if self.on_change:
                self.on_change(new)

    def _poll(self) -> None:
        while True:
            self._refresh()
            time.sleep(POLL_S)


def audio_node() -> str | None:
    """PipeWire node name of a connected phone's A2DP stream, if there is one."""
    import subprocess

    try:
        objs = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout)
    except Exception:
        return None
    for o in objs:
        props = o.get("info", {}).get("props", {})
        name = str(props.get("node.name", ""))
        if name.startswith("bluez_input.") or (name.startswith("bluez_") and props.get("media.class") in ("Audio/Source", "Stream/Output/Audio")):
            return name
    return None
