#!/usr/bin/env python3
"""Humdrum Bluetooth helper: runs on the board as root (systemd: humdrum-bt).

The app's container can't reach the system Bluetooth stack, so this does it:
  * keeps the adapter powered, named, discoverable and pairable, with an agent
    that accepts phones without a PIN ("just works" pairing)
  * reports the connected phone and what it's playing (BlueZ MediaPlayer1)
  * sends play / pause / previous / next to the phone (AVRCP)

It talks to the app over a Unix socket in the app's data folder, which the
container shares: one JSON request per line, one JSON reply per line.
  {"cmd": "status"}  {"cmd": "next"}  {"cmd": "discoverable", "on": false}

Only standard tools are used: bluetoothctl for the agent, busctl for D-Bus.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

SOCK = sys.argv[1] if len(sys.argv) > 1 else "/home/arduino/ArduinoApps/humdrum/data/bt.sock"
NAME = os.environ.get("HUMDRUM_BT_NAME", "HUMDRUM")
# The adapter is usually hci0, but when the WCN399x controller crashes and the
# kernel recovers it, it comes back as hci1 (seen 2026-11-17): find it each time.
DEVICE_RE = re.compile(r"/org/bluez/hci\d+/dev_[0-9A-F_]{17}")
visible = True  # "visible to new phones" as last asked for, put back after an adapter reset


def busctl(*args: str) -> dict | None:
    try:
        out = subprocess.run(["busctl", "--json=short", *args], capture_output=True, text=True, timeout=4)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {} if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def prop(path: str, iface: str, name: str):
    r = busctl("get-property", "org.bluez", path, iface, name)
    return r.get("data") if r else None


def objects() -> list[str]:
    try:
        out = subprocess.run(["busctl", "tree", "--list", "org.bluez"], capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip().startswith("/org/bluez/")]


def adapter(paths: list[str] | None = None) -> str:
    for p in paths if paths is not None else objects():
        if re.fullmatch(r"/org/bluez/hci\d+", p):
            return p
    return "/org/bluez/hci0"


POWER_RETRY_S = 60  # a dead controller is asked to power on at most this often


def restore_adapter(path: str) -> bool:
    """Put back what a reset controller loses (name, pairable, visible), changing
    only what is actually wrong. The WCN399x controller is fragile: it has hung on
    routine commands (0x0406 disconnect, 0x0c52 name/EIR update), and rewriting
    every setting each time a read failed is what made it crash on 2026-11-17.
    Returns False while the adapter isn't powered (nothing else can be set)."""
    if prop(path, "org.bluez.Adapter1", "Powered") is not True:
        return False
    wanted = (("Alias", "s", NAME, NAME), ("Pairable", "b", "true", True),
              ("DiscoverableTimeout", "u", "0", 0), ("Discoverable", "b", "true" if visible else "false", visible))
    for name, sig, value, expect in wanted:
        now = prop(path, "org.bluez.Adapter1", name)
        if now is not None and now != expect:  # None: couldn't read it, leave it alone
            print(f"adapter {path}: {name} was {now!r}, setting {value}", flush=True)
            busctl("set-property", "org.bluez", path, "org.bluez.Adapter1", name, sig, value)
    return True


class Agent:
    """bluetoothctl as a pairing agent that says yes to everything a phone asks."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, line: str) -> None:
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass

    def _run(self) -> None:
        while True:
            self.proc = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1)
            for cmd in ("power on", f"system-alias {NAME}", "agent NoInputNoOutput", "default-agent",
                        "pairable on", "discoverable-timeout 0", "discoverable on"):
                self.send(cmd)
            buf = ""
            while self.proc.poll() is None:
                ch = self.proc.stdout.read(1)
                if not ch:
                    break
                buf = (buf + ch)[-400:]
                buf = re.sub(r"\x1b\[[0-9;]*m|\x01|\x02", "", buf)  # colours and prompt markers
                # pairing confirmations and service authorisations from a phone
                if re.search(r"\(yes/no\):\s*$", buf):
                    self.send("yes")
                    buf = ""
            time.sleep(2)


def look_after_phones() -> None:
    """Trust every paired phone: BlueZ refuses an untrusted phone's music
    ("a2dp auth_cb: Access denied") unless an agent says yes each time.
    And turn each new music connection up to full volume: the phone scales the
    audio down before sending it, which leaves quiet passages crunchy, so
    Humdrum's own knob does the volume instead."""
    turned_up: set[str] = set()
    known, power_tried = "", 0.0
    while True:
        paths = objects()
        ad = adapter(paths)
        powered = prop(ad, "org.bluez.Adapter1", "Powered")
        if powered is False and time.monotonic() - power_tried > POWER_RETRY_S:
            # bluetoothd powers a working controller up itself; this is for one
            # that came back after a reset. Once a minute at most: each attempt
            # makes the kernel run the whole controller setup again.
            power_tried = time.monotonic()
            print(f"adapter {ad} is off: asking it to power on", flush=True)
            busctl("set-property", "org.bluez", ad, "org.bluez.Adapter1", "Powered", "b", "true")
        elif powered is True and (ad != known or prop(ad, "org.bluez.Adapter1", "Pairable") is False):
            if restore_adapter(ad):
                known = ad
        for p in paths:
            if (DEVICE_RE.fullmatch(p)
                    and prop(p, "org.bluez.Device1", "Paired") and not prop(p, "org.bluez.Device1", "Trusted")):
                busctl("set-property", "org.bluez", p, "org.bluez.Device1", "Trusted", "b", "true")
            if re.search(r"/fd\d+$", p) and p not in turned_up:
                turned_up.add(p)  # once per connection: the phone's buttons still work afterwards
                busctl("set-property", "org.bluez", p, "org.bluez.MediaTransport1", "Volume", "q", "127")
        turned_up &= set(paths)
        time.sleep(2)


def status() -> dict:
    paths = objects()
    ad = adapter(paths)
    st = {"powered": bool(prop(ad, "org.bluez.Adapter1", "Powered")),
          "discoverable": bool(prop(ad, "org.bluez.Adapter1", "Discoverable")),
          "connected": False, "device": "", "playing": False, "title": "", "artist": "",
          "_device_path": "", "_player": ""}
    for p in paths:
        if DEVICE_RE.fullmatch(p) and prop(p, "org.bluez.Device1", "Connected"):
            st.update(connected=True, device=prop(p, "org.bluez.Device1", "Alias") or "", _device_path=p)
            players = [q for q in paths if q.startswith(p + "/player")]
            if players:
                st["_player"] = players[0]
                st["playing"] = prop(players[0], "org.bluez.MediaPlayer1", "Status") == "playing"
                track = prop(players[0], "org.bluez.MediaPlayer1", "Track") or {}
                st["title"] = (track.get("Title") or {}).get("data", "")
                st["artist"] = (track.get("Artist") or {}).get("data", "")
            break
    return st


def handle(req: dict) -> dict:
    cmd = req.get("cmd")
    st = status()
    public = {k: v for k, v in st.items() if not k.startswith("_")}
    if cmd == "status":
        return public
    if cmd in ("play", "pause", "next", "previous", "playpause"):
        if not st["_player"]:
            return {"error": "no phone playing over Bluetooth"}
        method = {"play": "Play", "pause": "Pause", "next": "Next", "previous": "Previous"}.get(cmd)
        if cmd == "playpause":
            method = "Pause" if st["playing"] else "Play"
        r = busctl("call", "org.bluez", st["_player"], "org.bluez.MediaPlayer1", method)
        return {"ok": r is not None}
    if cmd == "discoverable":
        global visible
        visible = bool(req.get("on", True))
        busctl("set-property", "org.bluez", adapter(), "org.bluez.Adapter1", "Discoverable", "b", "true" if visible else "false")
        return {"ok": True}
    if cmd == "disconnect":
        if st["_device_path"]:
            busctl("call", "org.bluez", st["_device_path"], "org.bluez.Device1", "Disconnect")
        return {"ok": True}
    return {"error": f"unknown command {cmd!r}"}


def serve() -> None:
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o666)  # the app runs as the arduino user inside its container
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_client, args=(conn,), daemon=True).start()


def _client(conn: socket.socket) -> None:
    with conn:
        conn.settimeout(5)
        buf = b""
        try:
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            reply = handle(json.loads(buf or b"{}"))
        except Exception as e:  # keep serving whatever one client sends
            reply = {"error": str(e)}
        try:
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass


if __name__ == "__main__":
    Agent()
    threading.Thread(target=look_after_phones, daemon=True).start()
    serve()
