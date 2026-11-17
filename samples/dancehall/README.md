# Dancehall / sound-clash kit

Synthesised by `tools/make_samples.py` (oscillators and noise only, so they're free to use and share). 48 kHz mono 16-bit WAV, peaks at −1 dBFS. Upload them from the web UI.

| File | Length | What it is | Pad mode |
|---|---|---|---|
| `airhorn.wav` | 2.4 s | The classic *bap-bap-baaaap* triple blast | oneshot |
| `airhorn-long.wav` | 3.1 s | A single long blast | gate (hold it as long as you like) |
| `explosion.wav` | 4.7 s | Crack, fireball, sub thump, debris crackle, rumble | oneshot |
| `bomb-drop.wav` | 6.8 s | Falling whistle, then the explosion | oneshot |
| `fireworks.wav` | 2.6 s | Six cracking bangs with room echo | oneshot |
| `door-slam.wav` | 1.9 s | One hard bang with room echo | oneshot |
| `dub-siren.wav` | 5.9 s | Warbling, climbing dub siren into tape echo | oneshot |
| `laser.wav` | 1.7 s | *Pew-pew-pew* with echo | oneshot |
| `rewind.wav` | 2.0 s | The record spun back until it stops (*pull up!*) | oneshot |
| `bass-drop.wav` | 3.0 s | Sub boom sweeping down to 32 Hz | oneshot |
| `riser.wav` | 4.0 s | Noise-and-saw build-up with a hard stop: hit the drop right after | oneshot |
| `whistle.wav` | 2.2 s | Referee whistle, four short blasts and one long | oneshot |

To regenerate or tweak them (pitch, rhythm, lengths are all plain numbers in the script):

```bash
uv run tools/make_samples.py
```
