# Handoff: Humdrum audio quality on the Media Carrier

> **Follow-up (2026-11-25): period 256 rejected, lighter web replies, split outputs.**
>
> | Finding | Evidence | Change |
> |---|---|---|
> | `io_period` 256 isn't safe for a gig | With PipeWire realtime: an output underrun (and a 10-58 ms mic skip) every 10-40 s, 0 at 512. At 256 the engine has 5-10 ms per 512-frame block (depends on the mic and output ticks' phase); processing peaks at 7-9 ms, and the card's DMA moves audio in 480-frame (10 ms) bursts under the 256 quantum. It saved ~11 ms of ~122 | Stays 512 (`engine.IO_PERIOD` comment) |
> | Web replies held the GIL | FastAPI walks a returned dict in Python (`jsonable_encoder`): 65 ms for the 77 KB full state, during which audio blocks took 20-40 ms. `json.dumps` takes 12 ms. Still, each full state built and sent costs one 22-30 ms block: 70 KB of it is the pads' waveform peaks | `main._json` for `/api/state`, `/api/settings`, `/api/audio/debug`; Bluetooth changes (every track title) push `settings`, not the full state |
> | Slow blocks: who held the GIL | `Engine._note_slow`: a block over 12 ms records the other threads' stacks (after the write), in `/api/audio/debug` `slow`. It caught `WebUI.execute` in `jsonable_encoder` and `setup_state` | Kept: costs ~1 ms, only on slow blocks |
> | "Audio stopped" at every start | The stall watch compared against time 0 before the first block, and never cleared its message | Starts its clock on the first look; clears its own message on recovery |
> | Mic and music on separate jacks | Line out and ear out are fed from the left channel's DAC (UCM `Lineout.conf`/`Earphones.conf`: RX0 through HPHL only), so two signals at most: left = line out, ear out, combo tip; right = combo ring | `output_split` (`off`, `mic_line`, `music_line`): the mic on one channel, the music summed to mono on the other, a limiter per channel; line out and the combo jack are held on. Checked on the board: tones and the mic's sweep land on the right channel, the other at -240/-83 dB. And mixed, with only line out/ear out ticked, the mix is now summed to mono (line out was playing the music's left channel only) |
> | Mic knob dead until a replug | ~97 "corrupted Modulino reads" a second from 02:42:34 (one module failing every 10 ms poll), no click or event before it; the same on 2026-11-24 21:33. The sketch rejects a knob reading more than 40 steps from the last one but never moved `last`: once the knob restarted (counting from 0 again), every reading after it was rejected | `pollKnob`: three reads in a row that agree with each other become the new position (no turn reported). `bad_reads` now carries the latest module's address and why; main.py logs it at most every 10 s |
> | One 52 ms gap in the Bluetooth | In the `bluez_input` recording itself (PipeWire's realtime node), with a burst of slow app blocks at the same moment; not repeated in the next 50 s run (0 events in and out) | None: the radio (the hotspot shares the 2.4 GHz antenna) |

> **Mic delay, Bluetooth clicks and skips with the FX knob (2026-11-24).** After a gig: "noise on the Bluetooth", "skipping when playing with the FX levels", "significant delay on the mic". All three came from the audio thread falling behind, and the mic delay from what that left behind.
>
> | Finding | Evidence | Fix |
> |---|---|---|
> | Mic 163 ms late | Acoustic loop: a sweep played to the sink, heard by the mic beside the speaker, back in the sink's monitor 162.8 ms later (8 of 8 sweeps). Each output dropout adds its length for good: the mic keeps capturing while the card plays silence. One 50 ms stall added a period (iotest); after a gig both 4-period rings were full | `Engine._catch_up`: the audio in flight (mic waiting + output queued, from pyalsaaudio `avail()`) is held at 2 periods. A dry output shows as a *negative* fill until the next write; then the level is put back (stale mic audio dropped, or silence queued), judged by its minimum over 8 blocks. Backstop: its minimum over 16 blocks 2 periods over target. Each real dropout widens the target half a period, up to 24 ms |
> | ...and the rumble filter | Linear-phase low cut: 10 ms | `fx.HighPass`: 4th-order Butterworth at 60 Hz as complex one-pole sections summed in closed form (numpy, no scipy). Matches a per-sample recursion to 2e-7; 0.65 ms group delay at 200 Hz |
> | Skipping on the Bluetooth (gig log) | "bluetooth buffer at 234-260 ms: skipped ahead" every 3-6 s: the engine ran slower than real time, the mic overran, the music piled up | Everything below. 0 skips since |
> | FX knob made it skip | The Filter designed its FIR in the audio thread, 3.4 ms per instance, twice (L, R) per knob step: **14.9 ms per 10.7 ms block** while turning (profiled on the board) | All 481 knob positions designed once at start (`Filter.warm`, 0.9 s), shared by every instance: 4.1 ms per block turning |
> | Everything slower than it needed to be | Profiled per stage | Bluetooth copied sample for sample (PipeWire already locks the phone to the card's clock; the sinc resampler only runs if the level wanders 40 ms); slice-based `DelayLine`; flat Filter = a delay, no FFT; noise floor every 8th block. Per block with Bluetooth: reverb 6.6 -> 4.5 ms, theremin 7.0 -> 5.4 |
> | Stalls from the rest of the app | `json.loads` of `pw-dump` (160 KB, 15 ms with the GIL) every 2 s while no phone streams; a 77 KB state push every 0.15 s while a knob turns | `bt.audio_node` reads `pw-cli ls Node` (2 KB); knob turns and sliders push a `settings` message (the UI merges it); `gc.freeze()` after start-up (longest collection since: about 5 ms) |
> | Clicks on FX changes | Robot's carrier jumped phase at every knob step (it was `sin(2 pi f t)` with t from the start); mix levels stepped once per block; reverb tail cut at 0; Pitch jumped crossing its dead zone | Phase accumulator; `Effect._glide` ramps; equal-power crossfade into and out of shifting |
> | PipeWire missing deadlines | The arduino user's PipeWire (the card's) runs at normal priority: rtkit refuses a lingering user with no session ("Supervising 0 threads"). dbus-daemon at 25% of a core: the helper ran ~10 `busctl` per status, twice a second | `install.sh` (root, then reboot): `LimitRTPRIO=95` for `user@1000.service`, polkit rule for rtkit, `humdrum-cpu.service` (performance governor), helper reads everything with one `GetManagedObjects` |
> | No sound and no mic after the reboot that brought realtime in | PipeWire (now starting at nice -11) opened the card before its routing was on: kernel "MultiMedia1: ASoC: no backend DAIs enabled", PipeWire "playback open failed: Invalid argument", WirePlumber gave up on both nodes with the profile still on Headphones. The app's `pipewire:NODE=` target fell back to the dummy `auto_null` sink: open, no error, silence | `audio_ports.ensure_carrier_profile` switches the profile off and on when its nodes are missing; a watch thread in main.py reopens the devices if they're missing for two 10 s checks. Tested by destroying the sink node: back in ~15 s |
> | Laptop/phone could drop to call audio | macOS listed HUMDRUM as its **default input** (16 kHz mono, HFP): any app opening a mic would switch the link off A2DP | WirePlumber `bluez5.roles = [ a2dp_sink ]`, no HFP/HSP backend (`83-humdrum-a2dp-only.conf`) |
>
> **Still open.** (1) About 89 ms from the sink's monitor to the mic hearing the speaker (it was 83 ms at a 256 quantum and barely moved at 128): ALSA's own buffers account for ~30 ms (`/proc/asound/card0/pcm0p/sub0/status` delay 769 frames, `pcm2c` 703; both with 480-frame DMA periods), so ~55 ms is the Qualcomm DSP's pipeline, the speakers' own processing, or both. Not yet told apart: a wired earbud held to the mic would measure the board alone. (2) `io_period` 256: tested on 2026-11-25 with realtime in, kept at 512 (below).
>
> **How it was measured** (scripts were in the session's scratchpad; easy to rebuild):
> - *Glitches*: two-tone test (L 1 kHz, R 1.5 kHz) played from the laptop over Bluetooth; `pw-record` of the `bluez_input` node and of the sink monitor at once; high-pass above 4 kHz, 1 ms frames 20 dB over the median floor are events, plus runs of exact zeros. A click in both = from the radio, in the output only = the app's. A jump of 256 samples hides on the 1.5 kHz channel (32-sample period): look at both.
> - *Latency*: one `pw-record` (`node.autoconnect = false`) with `pw-link` from the sink's `monitor_FL` and the mic source's `capture_FL`, so both are on one clock; 60 ms log sweeps played to the sink; the sweep is subtracted from the monitor and its echo found by cross-correlation. Linking one recorder to both graphs merges them and disturbs the engine for a few seconds: measure a second time. Stop any other playback first.
> - *Dropouts*: `/api/state` audio stats (`out_underruns`, `mic_skipped_ms`, `io_ms`, `dsp_max_ms`, `gc_max_ms`), `/api/audio/debug` `trace` (ring levels per block), and `pw-top -b` ERR per node. Don't change `clock.force-quantum` live while Bluetooth plays: the 44.1 kHz node went to 17 000 errors.


> **Bluetooth "goes silent when playing" (2026-11-18): kernel bug, fixed by Arduino's 7.0.14 kernel.** On `7.0.0-g122c2c22d838` the first `qcom_geni_serial 4a8c000.serial: serial engine reports 0 RX bytes in!` left the UART to the WCN399x deaf for good (the RX DMA was never re-armed), so every HCI command timed out: all of the "controller crashes" above. Fix: `linux-image-7.0.14-arduino1+` (arduino-deb-images#18, upstream "serial: qcom_geni: Fix RX DMA stall when SE_DMA_RX_LEN_IN is zero"). Installed offline from Arduino's apt repo (the board has no internet while it runs the hotspot); systemd-boot picks the newest entry, 7.0.0 stays as fallback. After it: 7 RX events survived in 6 minutes of playback. `arduino-unoq` 0.2.1 still depends on the 7.0.0 package, so both stay installed.

> **Root cause found (2026-11-17, late): the carrier's PCMs mis-pack S24_LE.**
> The DSP treats each 32-bit word as a 16-bit sample in its top half. With
> PipeWire opening hw:0,0 and hw:0,2 as S24_LE (its default):
> - **Playback** reached the DAC **~48 dB too quiet with ~8 bits of resolution.**
>   That's the "hissy, compressed, quiet at any volume" Bluetooth sound, and
>   why the amp seemed to need so much gain. Measured with a 1 kHz tone at
>   -40 dBFS played straight to hw:0,0 and picked up by the mic through TRRS
>   crosstalk: S16_LE -101 dBFS every time, S24_LE -136..-140 (noise floor).
> - **Capture** came back **48 dB hot with only the low 8 bits of each sample**
>   (wrapping above 1/256 of full scale): every capture today had at most 256
>   distinct values on a 1/128 grid. hw:0,2 direct: S16_LE -78.5 dBFS =
>   S24_LE -30.5 dBFS on the same input. That's the old "mic is pure noise /
>   huge rumble / clips the codec" history, the -42 dBFS mic "hiss" the gate
>   opened on, and the real mic "clipping" at any gain.
> - **Fix:** WirePlumber rule `82-humdrum-s16.conf` (user config; installer
>   writes it too) sets `audio.format = "S16LE"` on both nodes. After it: mic
>   floor -92.8 dBFS (was about -43), and playback through PipeWire matches the
>   direct S16 test. **Playback is now ~48 dB louder at the same settings**, so
>   master was set to 0.01 before switching; raise it from there.
> - Earlier conclusions made on the broken path need re-checking: the mic
>   gains (ADC2/TX_DEC0, mic_gain_db, the rumble filter's reason), the
>   compander/LOHIFI "a bit better" results, and hp_gain. The HPH Volume steps
>   are still unmeasured: crosstalk didn't change between amp 1, 10 and 20, so
>   it couples in before the amp, and the TLV's 0.07 dB/step may even be
>   right.

Status on 2026-11-17. The open problem: **Bluetooth music through the combo
jack still sounds a bit noisy and compressed at low listening volume.** After
the last change (headphone amp in LOHIFI mode) it is "a bit better". The mic is
clean. (All of it has been committed since.)

## The setup

- Arduino UNO Q (QRB2210 Linux + STM32U585) on the UNO Media Carrier. Audio is
  the carrier's WCD937x-family codec, card `ArduinoImolaHPH` (card 0).
- PipeWire 1.4.2 / WirePlumber 0.5.8, running in the `arduino` user's session.
  The app runs in an App Lab Docker container (`humdrum-main-1`) and reaches
  PipeWire through `/run/user/1000/pipewire-0`.
- Listening on **Apple TRRS earphones** (also tried a second TRRS headset) in
  the combo jack ("headphones" in the app's carrier outputs). Very sensitive.
- Phone: Pixel 7a over A2DP. Codec **SBC, bitpool 53** (config bytes
  `[33,21,2,53]`). No AAC decoder in this PipeWire build, so SBC is the ceiling.

## Signal chain (Bluetooth music)

Updated 2026-11-17 (second session): stereo end to end, sinc resampler,
look-ahead limiter, float32 output. The codec is the **PM4125** (QRB2210's
PMIC codec, `sound/soc/codecs/pm4125.c`), not a WCD937x.

```
phone (AVRCP volume, set to 127 by host helper)
 -> SBC -> bluez_input.<mac>.2 (Audio/Source, never auto-linked)
 -> pw-record f32 STEREO 48 kHz        python/engine.py BtInput._loop
 -> jitter buffer 80 ms, drift follow   BtInput.read (32-tap windowed sinc, smoothed fill)
 -> x bt_level (+ pads x sample_level)  Engine block
 -> Bluetooth FX, one instance per channel (bt_effects = left, bt_effects_r = right)
 -> + mic x monitor_level (mono, both channels)
 -> x master -> Limiter (look-ahead 64 samples, ceiling 0.97, idle below it)
 -> Speaker float32 stereo -> PipeWire Headphones sink (S24_32LE, hw:0,0)
 -> RX macro: RX_RX0/RX1 Digital Volume (both held at 84 = 0 dB), compander OFF
 -> PM4125 HPH PA: "HPHL/HPHR Volume" 1..20 = hp_gain, compander OFF,
    "RX_HPH PWR Mode" LOHIFI -> combo jack
```

## Second session (2026-11-17): what was found and changed

| Finding | Evidence | Fix |
|---|---|---|
| Treble pumped and dulled | Catmull-Rom error at the real drift ratios: -27 dB at 10 kHz, -15 dB at 15 kHz. Simulated through `BtInput.read` at 80 ppm drift: the level of a 15 kHz tone swung 2.5 dB (18 kHz: 5.3 dB) at the drift beat rate, about 4 Hz, and averaged -1.2 dB | 32-tap Kaiser-windowed sinc, 2048 phases (`SINC` table in engine.py): about -74 dB at 10 kHz, 0.00 dB wobble. 0.9 ms/block on the board |
| Music flattened | `pw-record --channels 1` downmixed the phone's stereo | Stereo capture, stereo output; mic/pads/tones mono in both channels |
| "Compressed" loud passages | `soft_limit` bent every sample above 0.8. The user's `bt_level` was 1.5, so anything above -5.5 dBFS in the phone's (full-scale) audio was being waveshaped | `Limiter`: look-ahead gain limiter, bit-exact below 0.97 (tested), smooth 1.3 ms attack, 30 dB/s release. `audio.limit_db` shows when it works |
| Compander off only half applied | Carrier UCM (`codecs/pm4125/HeadphoneEnableSeq.conf`, `qcom-lpass/rx-macro/HeadphoneEnableSeq.conf`) turns all four compander switches ON whenever PipeWire activates the port. In the kernel, `rx_macro_set_compander` only stores the flag (applied at interpolator power-up) and `pm4125_codec_hphl_dac_event` writes COMP_CTL at DAC power-up. The app switched them off *after* opening the output, so the RX macro's digital compressor kept running until the next power cycle | `audio_ports.prepare_output()` now sets companders off and RX gains (with the HPH mode) *before* the output opens. Restart the app with ~12 s between stop and start so the card suspends and the amp powers down |
| 16-bit truncation | `(y*32767).astype(int16)` truncates toward zero | Output float32 (`Engine._to_device` converts if a device falls back) |
| CPU | Stereo + sinc cost more | `InputConditioner` used `np.percentile` twice a block (about 1.5 ms); replaced with `np.partition` (identical output). `Reverb` skips its tank at amount 0 (its output is dry there anyway). Measured old/new per 10.7 ms block: 3.9/4.9 ms with Bluetooth playing; 5.1/7.0 ms with a Bluetooth reverb at 0.6 |
| Bluetooth effects degraded music at tiny knob amounts | Pitch up/down and the Theremin pitch-shift 100% of the music at any amount > 0. The user's knob sat at Theremin 0.04 (about -0.5 semitone through the granular shifter, with no hand over the sensor) | `BT_FX_DEADZONE` 0.05 (two 0.02 knob clicks) is dry, then rescaled to full at 1.0 (`engine.bt_fx_amount`). Bit-identical dry output at 0.02 and 0.04 for all 8 effects |
| Mic gate open on hiss alone (49% of the time) | The headset hiss swings about 10 dB block to block (gate level p1 -70, p10 -52, p50 -45, max -42 dBFS after 6 dB gain). The floor was the 10th percentile of 2 s plus 6 dB, so -46, below the typical hiss. Speech also raised that 2 s floor and got cut mid-phrase | `InputConditioner`: floor from the 20th percentile of 10 s, open 10 dB above it, close 4 dB lower (hysteresis), hold 0.2 s. On the board's recorded noise plus synthetic speech: open on noise 4.6% (mostly the hold after phrases), speech -20/-30/-36 dBFS open 99/99/98%. Live: closed at -41..-46 dBFS, opens for voice at -12..-21 dBFS. `gate_db` (settings page) stays a minimum threshold |
| A real mic (via a TRRS splitter) clipped close up | Clipping happens in the codec, before any software gain: flat tops at full scale with TX_DEC0 78. Voice peaks measured about 18 dB over full scale there; ADC2 is already at its lowest, but TX_DEC0 has room (at 30 the peaks were -30 dBFS) | `mic_pad_db` (0-42, persisted, slider "mic input pad dB"): TX_DEC0 = 78 - pad. The engine steps it +6 dB on each clipped block (>= 0.98, at most every 0.3 s, off the audio thread) and never lowers it; it's deliberately **not** made up in software (that just moved the overload into the limiter: up to 19 dB of reduction at mic gain +25). Mic capture is now float32. This mic settled at 18-36 dB; the headset mic needs 0 |
| Hiccups when switching effects | The new effect was reset and cut to instantly: the old output stopped mid-wave (click) and the pitch effects read their emptied 43 ms delay line (silence; -160 dB dip measured switching to Pitch down) | `FxChain` (engine.py): the new effect is primed with the last 4096 input samples (`Effect.prime`) and the old one keeps running for an 11 ms equal-power crossfade. Click energy at switches went from -20..-45 dB to -70..-88 dB |
| FX-switch click persisted after the crossfade | Recording the output while switching: a 512-sample run of exact digital silence at the switch = an xrun. Each switch rebuilt and pushed the full state while holding the GIL: 22 ms of it recomputing 12 pads' waveform previews, 65-87 ms in all, while the audio thread needs ~7 of every 10.7 ms | Pad `peaks` cached when the audio is attached (slots.py); settings and knob changes push state through the 0.15 s coalescer; `sys.setswitchinterval(0.001)` so the audio thread gets the GIL back within 1 ms. After: 9 switches, no gaps; 30 s idle, no gaps |
| Bluetooth/pads Pixels meter dark below full volume | The meters read the mix after master, and master is now the earphones' -20 dB trim | Meters read before master (`level_play`, `level_mic_out`): music at the usual knob position lights 4-7 of 8 LEDs |
| One NaN silenced the output until a restart | The look-ahead limiter keeps gain state; a NaN made it NaN for good | `Engine._finite` replaces NaN/inf after each stage (mic effect, Bluetooth input, pads, Bluetooth effect), resets that stage, logs it once; the limiter resets a non-finite state |
| Music ran 9 cents sharp for minutes after every start | The reader thread filled the buffer to MAX_S (500 ms) while the engine was opening devices; the drift follower then drained the extra 420 ms at its 0.5% limit (2-3 minutes), trimming the front in small skips at first | Priming starts from the newest TARGET_S; a backlog more than `RESYNC_S` (150 ms) above the cushion is dropped with a 128-sample fade (`BtInput.resyncs`) |

Live check through the real chain (fake phone, see below): L 1 kHz / R 1.5 kHz
in, separation > 115 dB, level exactly input x bt_level, residual -76 dB
(1 kHz) / -70 dB (10 kHz), no dropouts.

Checked with the Pixel (same session, music playing, no effect on): output =
phone x 0.750 exactly (the bt_level), stereo width identical (side/mid
-18.7 dB both). With the drift follower's few-ppm time warp modeled (it moves
about 17 ppm), what the app adds is -65 dB below the music (-84 dBFS),
47-86 dB below it in every band. pw-top: no xruns on the output path. The
phone's SBC stream itself stops at about 17 kHz (-104 dB/Hz at 17-18 kHz).
The audio thread uses about 56% of a core (`top -H`); `dsp_ms` (wall clock,
including GIL waits) reads 7-8 ms of the 10.7 ms block while music plays. The
Bluetooth node now runs off the mic source's driver (`pw-top`), so it's on the
codec clock.

To compare the phone with the output, record both at once and allow a drifting
delay: a single fixed delay over a second leaves a residual of only -26 dB that
rises with frequency (the drift follower's time warp), which looks like added
treble noise but isn't.

HPH gain direction: PM4125 `HPHL Volume` is `SOC_SINGLE_TLV(..., 0, 20, 1 /* invert */, line_gain)`,
so control 20 = register 0. The TLV says control 0 = mute; whether register 20
is really mute or just the lowest step is unknown. Trying to measure the step
size through headset-mic crosstalk failed: crosstalk is below the mic noise
(-57 dBFS per octave band at 8-16 kHz).

Fake phone, for testing the Bluetooth path without a phone (the app picks up
any node named `bluez_input.*`):

```bash
# on the board, as arduino, XDG_RUNTIME_DIR=/run/user/1000
pw-loopback -n humdrumtest --channels 2 \
  --capture-props="{ media.class = Audio/Sink node.name = humdrumtest_sink }" \
  --playback-props="{ media.class = Audio/Source node.name = bluez_input.humdrumtest node.autoconnect = false }" &
sleep 4; pw-play --target humdrumtest_sink test.wav &
timeout 14 pw-record --raw --target alsa_output.platform-sound.Headphones__Headphones__sink \
  -P '{ stream.capture.sink = true }' --format f32 --rate 48000 --channels 2 - > /tmp/out.raw
# (adding node.dont-fallback to that monitor capture made it record only ~1 s)
```

Mic chain: codec ADC2 (0 = lowest analog gain) -> TX_DEC0 Volume 78 (-6 dB)
-> `RumbleFilter` + moving-average high-pass -> x mic_gain_db -> adaptive noise
gate (ignores the loudest 5% of samples) -> mic FX.

## Current settings on the board

At the start of the second session: `bt_level 1.5, sample_level 1.5, master 1.0,
hp_gain 1, monitor_level 0.75, mic_mute true, mic_gain_db 6, gate_db -54,
bt_fx_index 7 (Theremin) amount 0, block 512, carrier_outputs [headphones]`.
Earlier the user had turned hp_gain down to 1 (the amp's minimum) with
bt_level 0.4. bt_level above 1.0 on full-scale phone audio always needs the
limiter: for loudness, raising hp_gain keeps the digital path below full scale.

## What was wrong, and what fixed it (in order)

| Symptom | Cause found | Fix (file) |
|---|---|---|
| Phone pairs but won't connect | Phone not trusted, `a2dp auth_cb: Access denied` | Helper trusts paired devices over D-Bus every 2 s (`host/bluetooth/humdrum-bt.py`) |
| No Bluetooth audio node at all | WirePlumber 0.5 disables bluez without an active seat; the board is headless | `monitor.bluez.seat-monitoring = disabled` in `~arduino/.config/wireplumber/wireplumber.conf.d/80-humdrum-bt-noseat.conf` (installer writes it) |
| Phone audio grabbed by another server | lightdm's own PipeWire owns seat0 | `monitor.bluez = disabled` for lightdm (`/var/lib/lightdm/.config/...`, installer) |
| Music bypassed the app; "BT noise" was the mic | WP linked the phone straight to the speakers; ALSA `pipewire:NODE=` silently fell back to the mic when the phone node was missing | Rule `node.autoconnect = false` for `bluez_input.*` (81-humdrum-bt-route.conf); capture with `pw-record --target <node>` and `node.dont-fallback` (engine.py) |
| Crackle | Gaps: bursts played with no cushion | Jitter buffer with prefill, fades, drift-following resample (engine.py `BtInput.read`) |
| Rough sound | Drift control reacted to the raw fill every block: pitch wobble about 50 times a second | Fill smoothed over about 2 s. Simulated interference went from -7 to -10 dB to -48 to -59 dB below the signal |
| Dull treble | Linear interpolation | Catmull-Rom cubic |
| Crunchy quiet passages | Phone at half volume scales the audio before SBC | Helper sets transport `Volume = 127` once per connection; level set by the app's knob |
| Distortion on loud parts | `tanh` soft clip bends every level | `soft_limit`: linear to 0.8, then a smooth knee |
| Only the left earpiece, dead mic | TRRS plug not fully home (the Media Carrier jack needs extra force; see the Arduino forum thread 1460037, posts 8-10) | Physical. Pushing it fully in fixed it |
| Right channel +17 dB (or +40) | PipeWire's sink and route volume only drives `RX_RX1 Digital Volume` | App holds RX0/RX1 at 84 (0 dB) in `audio_ports.apply_ports`. **Don't set the sink or route volume.** |
| Mic "pure noise" | The app applied Arduino's manual gain (ADC2 5, TX_DEC0 100, about +23 dB). The headset input carries a big rumble below 50 Hz (-25 to -18 dBFS), which clipped inside the codec | Codec near unity (`MIC_HW_GAIN` ADC2 0, TX_DEC0 78); steep linear-phase `RumbleFilter` (fx.py); gain applied in software after filtering |
| Mic clipping on loud voice | No headroom at TX_DEC0 84 | TX_DEC0 78 (-6 dB), mic_gain_db 6 |
| Gate opening on crackle | RMS-based gate | Gate level ignores the top 5% of samples (fx.py `InputConditioner`) |
| Audio stack vanished, behaviour changed after restarts | `arduino` user **linger was off**: PipeWire only ran while someone was logged in | `loginctl enable-linger arduino` (done; the installer does it now) |
| BT sounded compressed, level pumped | Codec headphone **compander** on by default | `RX_COMP1/2` and `HPHL/HPHR_COMP` off in `apply_ports` (safe live) |
| Hiss at low volume | HPH amp in **ULP** mode | `RX_HPH PWR Mode = LOHIFI`, set before the output opens (`audio_ports.prepare_output`). Takes effect at amp power-up. "A bit better" |

## Tried and reverted (don't repeat)

- **Forcing the mic node to S32LE** with a WirePlumber rule: the source node
  failed to create, so no mic at all. The hardware format is S24_LE; Arduino's
  `arecord -f S32_LE` only works through `plughw` conversion. Reverted.
- **Setting the sink to 100% to "fix gain staging"**: it only raised the
  right channel (+40 dB). Reverted to RX0/RX1 = 84.
- **hp_gain default 5** (based on the wrong sink-volume theory): now 14 by
  default. The user chose 1.

## Remaining hypotheses for "noisy and compressed at low volume"

1. **Gain structure with very sensitive earphones.** hp_gain is at its
   minimum, so all further attenuation is digital, and quiet passages land near
   the amp's fixed noise floor. The HPH gain step size isn't reliably known:
   the control's TLV claims 0.07 dB per step, which looks wrong (assumed
   1.5 dB). Options: check the real step size (e.g. play a tone, record
   through line in); move more of the attenuation into a lower-noise analog
   stage if one exists; try line out with an amp, or ear out.
2. **Headset mic wiring adding noise.** On TRRS the mic shares the earpiece
   ground, and the mic bias and ADC stay powered even when the app mutes the
   mic. Test with plain TRS (3-pole) headphones. If that is cleaner, power down
   the codec mic path while muted (e.g. `ADC2_MIXER Switch` off, with care:
   the engine is clocked by the mic and has a stall fallback).
3. **Radio interference.** The board runs the Wi-Fi hotspot (AP, constant
   transmit) and Bluetooth on the same chip. Compare with
   `sudo humdrum-portal off`.
4. ~~Mono downmix~~: done in the second session (stereo end to end). SBC at
   bitpool 53 remains the codec ceiling: this PipeWire build (`1.4.2-1~qcom1`)
   has no aptX or AAC decoder plugin (`/usr/lib/aarch64-linux-gnu/spa-0.2/bluez5/`),
   so the Pixel can only send SBC.
5. Pixel-side processing: check Developer options -> Bluetooth audio codec
   (SBC) and disable "absolute volume" to compare.

## How to measure (all verified working)

```bash
# app API (USB): forward and read state
adb forward tcp:7310 tcp:7000
curl -s localhost:7310/api/state | python3 -m json.tool | less

# record the phone's raw audio, the mic, and what the app outputs (on the board)
adb shell 'export XDG_RUNTIME_DIR=/run/user/1000; \
  timeout 4 pw-record --raw --target alsa_output.platform-sound.Headphones__Headphones__sink \
  -P "{ stream.capture.sink = true, node.dont-fallback = true }" --format f32 --rate 48000 --channels 2 - > /tmp/o.raw'
# mic: --target alsa_input.platform-sound.Headphones__Headset__source --channels 1
# phone: --target bluez_input.<MAC>.2 --channels 1   (find it with pw-dump)
adb pull /tmp/o.raw .   # analyse with numpy (rms, peaks, spectrum)

# codec controls
adb shell 'amixer -c 0 contents'        # RX_RX0/1 Digital Volume, HPHL Volume, RX_COMP*, RX_HPH PWR Mode, ADC2, TX_DEC0
adb shell 'for t in $(busctl tree --list org.bluez | grep -E "fd[0-9]+$"); do busctl --json=short get-property org.bluez $t org.bluez.MediaTransport1 Configuration; done'
```

App diagnostics: the main page's settings -> system shows bt underruns and
buffer fill. The Modules page shows bus health.

## Gotchas

- `sudo` needs the board password, which the user types (never the agent).
  Root steps live in `host/bluetooth/install.sh` and `host/portal/install.sh`
  (copied to `~/humdrum-host/` on the board).
- For user-level PipeWire commands over adb, export `XDG_RUNTIME_DIR=/run/user/1000`
  (and `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus` for `systemctl --user`).
- **The WCN399x Bluetooth controller can hang** (2026-11-17, 14:40, uptime
  ~4.8 h): `hci0: command 0x0406 tx timeout`, `crash the soc to collect
  controller dump`, then `Reading QCA version information failed (-110)` on
  every power-on retry. The adapter then shows `Powered: no` and phones can't
  see HUMDRUM; `bluetoothctl power on` fails. Recovery needs root: rebind
  `echo serial0-0 | sudo tee /sys/bus/serial/drivers/hci_uart_qca/{unbind,bind}`
  or reboot. If the phone "forgot" the board meanwhile, also
  `bluetoothctl remove <phone MAC>` on the board (only works once powered) so
  stale keys don't break the new pairing. The UI now says "Bluetooth is off"
  when `powered` is false. After the rebind it crashed again within a minute:
  BlueZ's power-on ran a second "setting up wcn399x", the kernel logged
  `WARNING: drivers/iommu/dma-iommu.c:1243 at iommu_dma_unmap_phys`, then a
  memdump, and it recovered by itself as **hci1**. The helper was hard-coded
  to `/org/bluez/hci0` (so: app shows Bluetooth off, new phones never get
  trusted); it now finds the adapter and restores alias/pairable/visible when
  it reappears. A cold reboot is the reliable reset for the controller.
- **Restarting WirePlumber drops the phone.** It reconnects when the user taps
  HUMDRUM again.
- Changing codec switches while playing can stall the playback DMA for good
  (seen with the HPH and RDAC switches). Compander off is safe live; power mode
  changes need the amp to power down (stop the app and wait about 8 s).
- Deploy: `adb push <file> /home/arduino/ArduinoApps/humdrum/<file>`, then
  `adb shell 'arduino-app-cli app stop user:humdrum; arduino-app-cli app start user:humdrum'`.
- Repo rules: never add Claude attribution to commits; never commit the real
  hotspot password (`host/portal/wifi.local.env`, gitignored). Before pushing,
  check it isn't in any tracked file (exit 1 = clean):
  `PW=$(grep -E '^PASSWORD=' host/portal/wifi.local.env | cut -d= -f2-); git ls-files -z | xargs -0 grep -l -a -F -- "$PW"`.
  Commit or push only when the user asks.
