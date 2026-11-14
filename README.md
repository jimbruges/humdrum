# Humdrum

**An extendable sampler and voice changer based on the UNO Q and Media Carrier combined with Modulino modules.**

A voice changer and sample-pad instrument for the Arduino UNO Q in a flat, instrument-style case.

- **Mic in → live FX → line out.** Seven effects (pitch up, pitch down, robot, bitcrush, echo, reverb, radio). Turn the Modulino Knob for the amount, click it for the next effect, hold it for bypass.
- **Modulino Buttons become sample pads**, 3 per module, picked up automatically as you plug them in. The LED is on when a pad holds a sample. **Hold a pad to record** from the mic into it.
- **A Wi-Fi hotspot with a captive portal.** Scan the QR code embossed on the lid, and your phone joins and opens the web UI. There you upload samples, trim, pitch and loop them, and change settings.
- **LED matrix display** behind a window in the lid shows the current effect, the knob amount and the output level, `REC` while recording, and the **waveform of the sample you just triggered**, filling left to right as it plays.
- **Optional Modulino Buzzer** as a tone-only test speaker.
- **Optional Modulino Distance** adds a **Theremin** effect: hand height plays your voice's pitch. It appears in the effect list only while the sensor is plugged in.
- **Optional internal power bank bay**: a 93 × 61 × 14 mm 5000 mAh bank fits inside, its ports reachable through the right wall next to the UNO Q's USB-C.

![4-column case](enclosure/renders/render-4col.png)

---

## Hardware

| Part | Qty | Notes |
|---|---|---|
| Arduino UNO Q (2 GB) | 1 | |
| Arduino UNO Media Carrier (ASX00083) | 1 | Stacks under the UNO Q and provides the audio jacks |
| Modulino Knob | 1 | Fits any grid slot |
| Modulino Buttons | 1 to 11 | 3 pads each; use as many as your case has slots |
| Modulino Buzzer | 0 or 1 | Optional, tone-only |
| Modulino Distance | 0 or 1 | Optional; enables the Theremin effect. Peel off the protective film! |
| Qwiic cables (50–100 mm) | 1 per module | Daisy-chained under the lid through the cable slots |
| M3 heat-set inserts (M3 × 5.7, Ø4.0 hole) | 6 | Tray bosses |
| M3 × 6 button-head screws | 6 | Lid; meant to be visible |
| M3 × 6 thread-forming screws (or M3 + Ø2.7 pilot) | 4 | Media Carrier onto the tray posts |
| M3 spacers, height = carrier-to-UNO Q stack gap | 4 | Only if you want the UNO Q screwed down as well as plugged in |
| Clear or smoked acrylic, 33.6 × 20.6 × 1.5 mm | 1 | LED matrix window, glued into the rebate |
| Rubber feet, Ø12 × 1+ mm | 4 | Recesses in the tray |
| 5000 mAh power bank, 93 × 61 × 14 mm, ports on a short end | 0 or 1 | Optional; sits in the battery bay. Secure with hook-and-loop tape. |
| Short USB-C cable (bank → UNO Q, 5 V / 3 A) | 0 or 1 | Runs **outside** the case; both openings are on the right wall |
| 3.5 mm **TRS** cable → mixer (balanced) | 1 | Line out is **mono differential**: use a balanced input |
| Electret / headset mic on a 3.5 mm **TRRS** (CTIA) plug | 1 | Mic input is on the combo jack with bias. A dynamic mic needs a preamp. |

### Audio jacks (Media Carrier)

The carrier has three 3.5 mm jacks: **Line Out** (mono, balanced), **Mic In / Headphones** (TRRS combo) and **Ear Out**. Two of them, on the UNO Q's digital-header edge, point out of the case's **front wall**. The third faces into the case and isn't used.

> ⚠️ The datasheet doesn't say which physical jack is which. Plug in and check, then label the front wall to match. If the line out turns out to be the inward-facing jack, use a short 3.5 mm extension.

---

## Software layout

```
humdrum/                   App Lab app (copy this folder into App Lab)
├── app.yaml                 uses the WebUI brick
├── sketch/sketch.ino        MCU: Qwiic bus, pads, knob, buzzer, LED matrix
├── python/
│   ├── main.py              Bridge ↔ engine ↔ web API glue, settings, LEDs, matrix
│   ├── engine.py            real-time audio thread: mic → FX → out, sample voices, recording
│   ├── fx.py                the 7 effects (block-vectorised numpy/scipy)
│   ├── slots.py             sample storage (48 kHz mono WAV in /app/data/slots)
│   ├── display.py           8×13 matrix frames (rotated 180°: the board sits USB-C-right)
│   ├── buzzer.py            pitch tracker + Modulino Buzzer modes
│   └── requirements.txt     scipy
├── assets/                  web UI (index.html, app.css, app.js, bundled socket.io)
├── host/portal/             captive-portal hotspot installer, run once on the board
├── tools/qr_emboss.py       regenerates the lid QR geometry from your hotspot credentials
└── enclosure/               CAD: STEP + STL per case width, master .sc design
```

### How pads are numbered

Each newly plugged Buttons module arrives at the factory address `0x3E`. The sketch moves it to the next free address in `0x40–0x5F` (stored in the module's flash) and blinks its LEDs three times to confirm. Pads are numbered by address, so **the order you first plug modules in is the pad order**.

- **Add modules one at a time** while the unit is on, about a second apart. Two brand-new modules plugged in together would both move to the same address.
- **To renumber**, go to web UI → settings → system → *reset button addresses*, unplug all Buttons modules, then plug them back in in the order you want.
- Samples are stored by pad number, so they stay put when a module is unplugged and come back when it returns.

### Pad behaviour

| Action | Result |
|---|---|
| Tap | Plays the sample (one-shot / gate / loop, set per pad in the web UI). The LED matrix shows its waveform, bright where it has played and dim ahead, then returns to the effect display 0.25 s after it ends. Loops keep the waveform up until stopped. |
| Hold (600 ms, adjustable) | Starts recording from the mic into that pad. LED blinks fast and the matrix shows `REC`. Release to stop. |
| LED | Off = empty · on = loaded · flicker = playing · fast blink = recording |

Recordings are auto-trimmed and normalised (both can be switched off), and can be recorded dry or with FX. *Rec over loaded pads* can be turned off to protect your samples.

### Theremin (optional Modulino Distance)

Plug in a Modulino Distance and a ninth effect, **THR**, joins the knob cycle and the web UI. Unplug it and THR disappears; if it was selected, Humdrum falls back to the first effect.

- **Your voice** is pitch-shifted by hand height, up to an octave either way at full amount. Closer is higher, as on a real theremin, and it glides rather than jumping.
- **An "ether" tone** (a sine with slow vibrato) follows the hand across three octaves, 110–880 Hz, at your voice's loudness. Hum to play it.
- **The knob** sets how much of both you get; 0 is fully dry.
- **The LED matrix** shows `THR`, and the row above the knob bar shows hand height instead of output level.
- **Range:** *theremin near mm* (default 40) and *theremin far mm* (default 350) in settings set the distances that count as closest and farthest. Nothing in range counts as far.

The sensor's ST driver (VL53L4CD, or VL53L4ED on some boards) is vendored in `sketch/src/` under its BSD licence, so App Lab can build the sketch on a board with no internet.

### Modulino Buzzer (optional)

The buzzer only plays a frequency for a set time on a piezo, so it can't reproduce audio. Choose a mode in settings:

- **pads**: each pad beeps at its own pitch (chromatic, pad 1 = middle C).
- **follow**: the output's dominant pitch is tracked and played, so your voice or a melodic sample comes out as a buzzy tone. Unpitched sounds stay silent.

---

## Deploying

1. **App.** Copy `humdrum/` into App Lab (or onto the board under `~/ArduinoApps/`) and run it. App Lab builds the sketch and installs `scipy` from `requirements.txt`.
2. **Audio devices.** The defaults are `jack:1` in and `jack:1` out. If you hear nothing, open the web UI → settings and try `jack:2` / `jack:3`, or an ALSA name (`plughw:CARD=…,DEV=…`). *System → reopen audio* applies the change. The current status appears under *system*.
3. **Hotspot + captive portal.** On the board (over SSH or `adb shell`):
   ```bash
   cd humdrum/host/portal && sudo ./install.sh
   ```
   ```bash
   sudo humdrum-portal on
   ```
   This creates a NetworkManager access point using the SSID and password in `host/portal/wifi.local.env` if you've made one, otherwise the public demo credentials in `host/portal/wifi.env`. **They must match the QR embossed on your lid** (see *Your own Wi-Fi password* below). It also sets up wildcard DNS, the RFC 8910 portal option, and a port-80 redirector to the web UI on port 7000. The UNO Q has one Wi-Fi radio, so while the hotspot is on the board is off your home network. Run `sudo humdrum-portal off` to go back.

---

## Enclosure

The case is a flat, Teenage-Engineering-style slab: a dark tray, a light lid, exposed button-head screws and an orange accent on the snap standoffs. The electronics strip is always on the right. It carries the LED matrix window, the engraved legend and the QR code. The Modulino grid grows to the left in **columns of 3 slots** (48 × 32 mm pitch, holes on the Modulino's 32 × 16 mm pattern). Every slot has Qwiic pass-through slots on both sides, so the cables run hidden under the lid.

| Columns | Slots | Max pads (knob in one slot) | Case W × D × H | Fits a 256 mm bed? |
|---|---|---|---|---|
| 1 | 3 | 6 | **140 × 124 × 36 mm** | ✅ |
| 2 | 6 | 15 | **188 × 124 × 36 mm** | ✅ |
| 3 | 9 | 24 | **236 × 124 × 36 mm** | ✅ |
| 4 | 12 | 33 | **284 × 124 × 36 mm** | ❌ needs ≥ 290 mm (e.g. Prusa XL, Bambu H2D) |

Files: `enclosure/<n>col/` has the tray STL, lid STL and STEP. `enclosure/humdrum-snap-standoff.stl` is shared by all widths (print 4 per Modulino). `enclosure/humdrum-enclosure.sc` is the master SolidCraft design: change the `cols` parameter to regenerate.

### Right wall: USB-C and battery ports

Both openings are on the **right wall**, so all power cables leave from one side:

- **UNO Q USB-C**: a 14 × 7.5 mm slot near the front of the wall, 9.2–23.2 mm back from the board's front edge, centred 1.5 mm above the UNO Q's top surface. The position comes from the datasheet's top view, with about 1 mm of slack each way.
- **Power bank ports**: a 44 × 12 mm slot near the front of the same wall. It sits between the two right-hand screw bosses, which also act as end stops for the bank.

### Battery bay (optional)

A power bank up to **93 × 61 × 14 mm** lies on the floor along the rear of the case, behind the UNO Q, **ports facing the right wall** a few cm behind the USB-C. It sits under the QR area and the right-hand Modulino column. The rear wall, two ribs on the board side and one end rib (8 mm tall, 0.6 mm clearance) hold it in place; add a strip of hook-and-loop tape. A short USB-C cable loops **outside** the case from the bank's port to the UNO Q's USB-C, a few cm apart on the same wall. So the bank stays optional, and the unit runs from any USB-C supply without it.

- The bay is in the same place at every width (1–4 columns), and checked clash-free against the tray at 1 and 4 columns.
- The bank sits beside the PCB stack, about 1 mm clear of the Media Carrier's inward-facing jack.
- The bank's port end stops about 11 mm inside the wall, so normal plugs reach; very short stubby adapters may not.
- To make room for the bank, the rear-middle lid screw sits at x = 28 mm, and the rear-right screw takes the place of the front-right one (the board now fills that corner).
- Check that your bank can supply the UNO Q (5 V, up to 3 A at peak). Some banks switch off at low current draw.

### Qwiic clearance

The UNO Q's Qwiic connector (QWIIC1) is on the board edge that faces the Modulino grid, about 24–27 mm above the floor. The space where the plug goes in (about 11 × 7 mm, up to the lid) is checked clear of the tray and the lid. A dedicated 10 × 5 mm **Qwiic pass-through** in the lid, just left of the LED matrix window, sits directly above the connector, so the first cable can go straight up to the nearest Modulino.

![Right wall](enclosure/renders/render-right-wall.png)
![Layout: UNO Q front-right, power bank rear-right](enclosure/renders/render-layout.png)

### Printing

- **Tray**: print open side up, no supports. PETG or ABS/ASA preferred; PLA is fine if the case stays out of the sun.
- **Lid**: print **top face up** so the QR modules and engraving come out crisp. Holes and slots are vertical, so no supports needed.
- **Snap standoffs**: upright, 0.12–0.16 mm layers, PETG for springiness. They click into the lid from above; the Modulino's Ø3.2 holes click onto the top pins.

### Colouring the embossed QR

The QR's dark modules stand 0.8 mm up from a recessed 42 × 42 mm field and finish **flush with the lid top**. Swipe a Sharpie or paint marker across the tops: the recessed quiet zone and gaps stay light. For a cleaner result, put a **filament change at Z = 35.2 mm** (lid printed top up). The modules then print dark, along with the top 0.8 mm of the lid, while the recessed field stays light, so the code still scans.

The QR is a standard `WIFI:` code (v3-M, 29 × 29 modules at 1.25 mm) for the hotspot's SSID and password.

### Your own Wi-Fi password

The published lid files carry a **public demo** network: SSID `HUMDRUM`, password `humdrum-demo` (in `host/portal/wifi.env`). That's fine for trying things out, but anyone who reads this repo can join a hotspot using it. Before building a unit you'll keep:

1. Copy `host/portal/wifi.env` to `host/portal/wifi.local.env` and set your own `SSID` and `PASSWORD` (8+ characters). This file is gitignored.
2. Run `sudo ./install.sh` on the board. It prefers `wifi.local.env`.
3. Run `python3 tools/qr_emboss.py`. It reads `wifi.local.env` and writes `enclosure/local/qr_emboss.json` and `qr_emboss.stl` (also gitignored), so your password never ends up in a commit.
4. Make a lid with your QR on it. In `humdrum-enclosure.sc`, delete the `QrSketch` and `QrEmboss` features, recreate them from the rectangles in `enclosure/local/qr_emboss.json` (one rectangle each, then extrude 0.8 mm up from the recess floor and join to the lid), and export the lid.

`python3 tools/qr_emboss.py --demo` regenerates the published demo files in `enclosure/`.


### Fit notes: check these before a final print

Positions come from the UNO Q and Media Carrier datasheet drawings. The board sits in the front-right corner, rotated 180° from the usual UNO orientation (USB-C to the right wall, digital header and jacks to the front):

- **USB-C** cut-out: right wall, 14 × 7.5 mm, spanning 9.2–23.2 mm back from the board's front edge, 1.5 mm above the UNO Q's top surface.
- **Jacks**: two Ø8 mm holes in the front wall, at 19.3 and 34.3 mm from the board's left edge, 17.3 mm above the floor's underside. Measured off the datasheet drawing; the edge was confirmed on a real Media Carrier.
- **Stack height**: assumes a 7.6 mm gap from carrier to UNO Q, putting the top of the UNO Q headers 0.5 mm under the lid. **This is the one number not stated in the datasheets.** If your stack is taller, raise `Ht`; if shorter, the jacks and USB-C sit a little high in their holes.
- **LED matrix window**: 30 × 17 mm, over the matrix at its datasheet position.

Print the tray once in draft quality to check the jacks and USB-C before printing the final parts.

---

## What's been verified

- **Sketch** compiles for `arduino:zephyr:unoq`: 13 % flash, 17 % RAM.
- **Python** effects, slots, engine, recording, loop/gate/one-shot and the pitch tracker were tested offline (`numpy`/`scipy`). A full block takes about 0.1 ms of a 10.7 ms budget on a desktop CPU. The QRB2210's A53 cores will be several times slower, which still leaves plenty of headroom.
- **Web UI + REST API** were run end to end against a stub of the App Lab runtime: upload with in-browser decode, edit, swap, delete, settings, and phone-width layout.
- **CAD**: every feature was checked against computed volumes, all 4 widths regenerate with no errors, the exported STL sizes match the table, and the QR raster round-trips to the exact symbol.

**Not yet tested on real hardware:** audio device naming and latency on the Media Carrier, Modulino auto-addressing with several real modules, the hotspot/portal on the board, and physical fit.
