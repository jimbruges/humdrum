#!/usr/bin/env bash
# Humdrum Bluetooth audio for the UNO Q (run once, on the board, as root).
#
#   sudo ./install.sh                 # the board shows up as "HUMDRUM"
#   sudo NAME="My Humdrum" ./install.sh
#
# What it sets up:
#   * PipeWire/WirePlumber: a phone's music arrives as an input the app records
#     (so it gets Humdrum's own Bluetooth FX and volume), instead of playing
#     straight to the speakers
#   * humdrum-bt, a small service that keeps the board discoverable, accepts
#     phones without a PIN and passes play/pause/previous/next to the phone
#   * realtime priority for the app's audio server, and the CPU at full speed
#     (clicks and a growing mic delay otherwise)
#   * Bluetooth as a music receiver only (A2DP), no headset profile
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="${NAME:-HUMDRUM}"
APP_DIR="${APP_DIR:-/home/arduino/ArduinoApps/humdrum}"
SOCK="$APP_DIR/data/bt.sock"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo $0)"; exit 1; }
command -v bluetoothctl >/dev/null || { echo "bluetoothctl not found (apt install bluez)"; exit 1; }
command -v busctl >/dev/null || { echo "busctl not found"; exit 1; }
install -d -o arduino -g arduino "$APP_DIR/data"

# The app's audio (PipeWire, WirePlumber) runs in the arduino user's session.
# Keep that session running with nobody logged in, or the board loses all
# audio when the last login (an adb or SSH shell) closes.
loginctl enable-linger arduino

echo "==> realtime priority for the audio"
# That session's PipeWire (the one with the sound card) runs from a lingering
# systemd user manager, with no login. rtkit only grants realtime priority inside
# an active session, so PipeWire's audio thread ran as an ordinary process and
# missed its 5-10 ms deadlines whenever the CPU was busy: clicks, and a mic that
# fell further behind with each one. With these limits PipeWire sets its own
# realtime priority, as it does for a desktop login. (Takes effect after a reboot.)
APP_UID=$(id -u arduino)
install -d "/etc/systemd/system/user@$APP_UID.service.d"
cat > "/etc/systemd/system/user@$APP_UID.service.d/90-humdrum-realtime.conf" <<CONF
[Service]
LimitRTPRIO=95
LimitNICE=-19
LimitMEMLOCK=infinity
CONF
# and in case PipeWire asks rtkit instead of setting it itself
if [[ -d /etc/polkit-1/rules.d ]]; then
  cat > /etc/polkit-1/rules.d/90-humdrum-rtkit.rules <<'RULES'
// Humdrum: the app's audio server (user arduino) runs without a login session;
// let rtkit give it realtime priority all the same.
polkit.addRule(function (action, subject) {
  if ((action.id == "org.freedesktop.RealtimeKit1.acquire-real-time" ||
       action.id == "org.freedesktop.RealtimeKit1.acquire-high-priority") && subject.user == "arduino") {
    return polkit.Result.YES;
  }
});
RULES
fi

echo "==> CPU cores at full speed"
# The cores scale their clock with the load (schedutil). The audio thread works
# in short bursts, about half a core, and a block that landed on a core clocked
# down to 0.6-1 GHz took two or three times as long: late audio.
cat > /etc/systemd/system/humdrum-cpu.service <<'UNIT'
[Unit]
Description=Humdrum: CPU cores at full speed, for steady audio timing

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > "$g"; done'
ExecStop=/bin/sh -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo schedutil > "$g"; done'

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now humdrum-cpu.service

# Classic Bluetooth only. The board's WCN399x controller hangs on routine HCI
# commands now and then and needs a reboot (four times on 2026-11-17; one was in
# LE privacy/scan commands). Humdrum only uses classic profiles (A2DP, AVRCP),
# so leaving LE off removes a whole class of commands it can choke on.
if grep -qE '^\s*ControllerMode\s*=' /etc/bluetooth/main.conf; then
  sed -i -E 's/^\s*ControllerMode\s*=.*/ControllerMode = bredr/' /etc/bluetooth/main.conf
else
  sed -i -E 's/^#\s*ControllerMode\s*=.*/ControllerMode = bredr/' /etc/bluetooth/main.conf
fi

echo "==> phone audio as an input for the app"
# WirePlumber 0.5 reads .conf files; 0.4 read .lua ones (0.5 warns about those).
install -d /etc/wireplumber/wireplumber.conf.d
cat > /etc/wireplumber/wireplumber.conf.d/90-humdrum-bt.conf <<CONF
# Humdrum: incoming A2DP (a phone playing music) becomes an Audio/Source node
# that the app records and mixes, rather than a stream sent to the speakers.
monitor.bluez.properties = {
  bluez5.media-source-role = input
}
CONF
if wireplumber --version 2>/dev/null | grep -q "libwireplumber 0\.4"; then
  install -d /etc/wireplumber/bluetooth.lua.d
  cat > /etc/wireplumber/bluetooth.lua.d/90-humdrum-bt.lua <<'LUA'
-- Humdrum: phone audio arrives as an input the app records (WirePlumber 0.4)
bluez_monitor.properties["bluez5.media-source-role"] = "input"
LUA
else
  rm -f /etc/wireplumber/bluetooth.lua.d/90-humdrum-bt.lua
fi

# The board runs headless: the arduino user's audio session never has an active
# login seat, and WirePlumber leaves Bluetooth audio off without one.
APP_USER_HOME=$(getent passwd arduino | cut -d: -f6)
install -d -o arduino -g arduino "$APP_USER_HOME/.config" "$APP_USER_HOME/.config/wireplumber" \
  "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d"
cat > "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d/80-humdrum-bt-noseat.conf" <<CONF
# Humdrum: the board runs headless, so the arduino user never has an active
# login seat; without this WirePlumber leaves Bluetooth audio switched off.
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
CONF
chown arduino:arduino "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d/80-humdrum-bt-noseat.conf"
# The phone's music is only for the app, which records it itself (pw-record):
# never connect it to anything automatically, e.g. straight to the speakers.
cat > "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d/81-humdrum-bt-route.conf" <<'CONF'
monitor.bluez.rules = [
  {
    matches = [ { node.name = "~bluez_input.*" } ]
    actions = { update-props = { node.autoconnect = false } }
  }
]
CONF
chown arduino:arduino "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d/81-humdrum-bt-route.conf"
# The carrier's PCMs (hw:0,0 playback, hw:0,2 capture) pack S24_LE wrongly: the
# DSP treats each 32-bit word as a 16-bit sample in its top half. Capture came
# back 48 dB hot with only its low 8 bits (wrapping above 1/256 of full scale);
# playback came out 48 dB quiet with only 8 bits left (the "hissy, compressed,
# quiet" Bluetooth sound). Measured 2026-11-17 through hw:0,x directly. S16_LE is
# packed correctly, so PipeWire opens both in it.
rm -f "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d/82-humdrum-mic-s16.conf"
cat > "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d/82-humdrum-s16.conf" <<'CONF'
monitor.alsa.rules = [
  {
    matches = [
      { node.name = "alsa_input.platform-sound.Headphones__Headset__source" }
      { node.name = "alsa_output.platform-sound.Headphones__Headphones__sink" }
    ]
    actions = { update-props = { audio.format = "S16LE" } }
  }
]
CONF
chown arduino:arduino "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d/82-humdrum-s16.conf"
# A music receiver only: no headset (HFP/HSP) role. With it, HUMDRUM offered
# phones and laptops a microphone too: a Mac made it its default input, and the
# moment any app opened a mic the link would drop from A2DP stereo to 16 kHz mono
# call audio. A phone call would also have gone out through the PA.
cat > "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d/83-humdrum-a2dp-only.conf" <<'CONF'
monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink ]
  bluez5.hfphsp-backend = "none"
}
CONF
chown arduino:arduino "$APP_USER_HOME/.config/wireplumber/wireplumber.conf.d/83-humdrum-a2dp-only.conf"

# The login screen (lightdm) runs its own audio session, which does own the seat
# and would grab the phone's music before the app's session can. Keep it off Bluetooth.
if LIGHTDM_HOME=$(getent passwd lightdm | cut -d: -f6) && [[ -n "$LIGHTDM_HOME" ]]; then
  install -d -o lightdm -g lightdm "$LIGHTDM_HOME/.config" "$LIGHTDM_HOME/.config/wireplumber" \
    "$LIGHTDM_HOME/.config/wireplumber/wireplumber.conf.d"
  cat > "$LIGHTDM_HOME/.config/wireplumber/wireplumber.conf.d/90-humdrum-no-bt.conf" <<CONF
# Humdrum: Bluetooth audio belongs to the app's session (user arduino), not the login screen
wireplumber.profiles = {
  main = {
    monitor.bluez = disabled
  }
}
CONF
  chown lightdm:lightdm "$LIGHTDM_HOME/.config/wireplumber/wireplumber.conf.d/90-humdrum-no-bt.conf"
  pkill -u lightdm -x wireplumber || true  # its user manager starts it again with the new setting
fi

echo "==> humdrum-bt service ($NAME)"
install -d /usr/local/lib/humdrum
install -m 0755 "$HERE/humdrum-bt.py" /usr/local/lib/humdrum/humdrum-bt.py
sed -e "s|@NAME@|$NAME|g" -e "s|@SOCK@|$SOCK|g" "$HERE/humdrum-bt.service" > /etc/systemd/system/humdrum-bt.service
systemctl daemon-reload
systemctl enable --now bluetooth.service humdrum-bt.service
systemctl restart humdrum-bt.service

echo "==> restarting the audio session so the new setting is used"
systemctl --machine=arduino@.host --user restart wireplumber.service 2>/dev/null \
  || sudo -u arduino XDG_RUNTIME_DIR=/run/user/$(id -u arduino) systemctl --user restart wireplumber.service \
  || echo "   (restart WirePlumber yourself, or reboot)"

cat <<DONE

Done. Reboot for the realtime audio priority to take effect:
  sudo reboot
Then, on your phone or laptop, connect to "$NAME". If it was paired before,
"forget" it there and pair again, so it stops treating it as a headset.
The app shows the phone under settings -> bluetooth once it's connected.
DONE
