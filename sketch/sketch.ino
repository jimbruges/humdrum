// Humdrum — MCU side (STM32U585)
//
// Owns everything on the Qwiic bus and the on-board LED matrix:
//   * discovers any number of Modulino Buttons (3 pads each), one Modulino Knob,
//     an optional Modulino Buzzer (a tone-only "speaker" for testing) and an
//     optional Modulino Distance (hand height for the theremin effect)
//   * gives each newly plugged Buttons module its own I2C address automatically
//   * reports pad / knob events to Linux over the Bridge
//   * shows pad states on the button LEDs and frames sent by Linux on the matrix
//
// Linux (python/main.py) is the source of truth for what is loaded where; this
// sketch only reports input and renders what it is told.

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include <Wire.h>
#include <vector>

// ST's time-of-flight drivers, vendored in src/ so the sketch builds offline.
// Modulino Distance boards carry either a VL53L4CD or a VL53L4ED.
#include "src/vl53l4cd/vl53l4cd_class.h"
#include "src/vl53l4ed/vl53l4ed_class.h"

// ---------------------------------------------------------------------------
// Bus layout
// ---------------------------------------------------------------------------
// Modulinos answer every read with their "pinstrap" byte first, which tells
// us the module type no matter which address it has been moved to.
static const uint8_t PINSTRAP_BUTTONS = 0x7C;
static const uint8_t PINSTRAP_KNOB_A = 0x74;
static const uint8_t PINSTRAP_KNOB_B = 0x76;
static const uint8_t PINSTRAP_BUZZER = 0x3C;
// The Distance module is a bare ST sensor, not a Modulino MCU: fixed address and
// no pinstrap byte, so it's handled separately from the scan.
static const uint8_t TOF_ADDR = 0x29;

static const uint8_t BUTTONS_DEFAULT_ADDR = 0x3E;  // factory address
// New Buttons modules are moved into this range, in plug-in order, so pad
// numbering follows the order you add modules.
static const uint8_t BUTTONS_FIRST_ADDR = 0x40;
static const uint8_t BUTTONS_LAST_ADDR = 0x5F;
static const uint8_t RESERVED_ADDRS[] = { 0x44 };  // Modulino Thermo is fixed here

static const int MAX_MODULES = 16;  // 48 pads
static const int PADS_PER_MODULE = 3;
static const int MAX_PADS = MAX_MODULES * PADS_PER_MODULE;

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------
static const uint32_t SCAN_INTERVAL_MS = 1000;
static const uint32_t POLL_INTERVAL_MS = 4;
static const uint32_t KNOB_REPORT_MS = 15;
static const uint32_t KNOB_LONG_MS = 700;
static const uint32_t DEBOUNCE_MS = 12;
static const uint32_t TOF_POLL_MS = 10;
static const uint32_t TOF_REPORT_MIN_MS = 25;   // at most ~40 updates/s over the Bridge
static const uint32_t TOF_REPORT_MAX_MS = 250;  // and at least 4/s so Linux knows it's alive

// Pad LED states, as sent by Linux in set_states()
enum PadState : uint8_t {
  PAD_EMPTY = 0,
  PAD_LOADED = 1,
  PAD_RECORDING = 2,
  PAD_ARMED = 3,    // hold reached, waiting for recording to start
  PAD_PLAYING = 4,  // loaded + currently sounding
};

struct ButtonsModule {
  uint8_t addr;
  bool down[PADS_PER_MODULE];
  uint32_t changedAt[PADS_PER_MODULE];
  uint32_t downAt[PADS_PER_MODULE];
  bool holdSent[PADS_PER_MODULE];
  uint8_t ledMask;  // last mask written, 0xFF = unknown
};

Arduino_LED_Matrix matrix;

ButtonsModule modules[MAX_MODULES];
int moduleCount = 0;

uint8_t knobAddr = 0;  // 0 = not present
uint8_t buzzerAddr = 0;

VL53L4CD* tofCD = nullptr;  // whichever variant answered
VL53L4ED* tofED = nullptr;
int16_t handMm = -1;  // -1: nothing in range
int16_t handReported = -2;
uint32_t lastTofPoll = 0;
uint32_t lastTofReport = 0;
int16_t knobLast = 0;
int32_t knobAccum = 0;
bool knobDown = false;
uint32_t knobDownAt = 0;
bool knobLongSent = false;

uint8_t padStates[MAX_PADS];
uint32_t holdMs = 600;

uint32_t lastScan = 0;
uint32_t lastPoll = 0;
uint32_t lastKnobReport = 0;
bool topologyDirty = true;
// After reset_addresses() every module sits on the factory address at once;
// don't enrol until the user has unplugged them all, or they'd all move together.
bool enrolHold = false;

// Frames from Linux arrive on the Bridge thread; hand them to loop()
struct k_mutex frameMutex;
uint8_t pendingFrame[104];
bool framePending = false;

// ---------------------------------------------------------------------------
// Raw Modulino I/O
// ---------------------------------------------------------------------------

bool probe(uint8_t addr) {
  Wire1.beginTransmission(addr);
  return Wire1.endTransmission() == 0;
}

// Reads `n` payload bytes; returns the pinstrap byte through `pinstrap`.
bool modRead(uint8_t addr, uint8_t* buf, int n, uint8_t* pinstrap = nullptr) {
  int got = Wire1.requestFrom(addr, (uint8_t)(n + 1));
  if (got < n + 1) {
    while (Wire1.available()) Wire1.read();
    return false;
  }
  uint8_t p = Wire1.read();
  if (pinstrap) *pinstrap = p;
  for (int i = 0; i < n; i++) buf[i] = Wire1.read();
  while (Wire1.available()) Wire1.read();
  return true;
}

void modWrite(uint8_t addr, const uint8_t* buf, int n) {
  Wire1.beginTransmission(addr);
  Wire1.write(buf, n);
  Wire1.endTransmission();
}

// Same protocol as the Modulino AddressChanger utility: the new address is
// stored in the module's flash.
bool changeAddress(uint8_t from, uint8_t to) {
  uint8_t data[40] = { 'C', 'F', (uint8_t)(to * 2) };
  modWrite(from, data, sizeof(data));
  delay(500);
  return probe(to);
}

void flashModule(uint8_t addr) {
  const uint8_t on[3] = { 1, 1, 1 }, off[3] = { 0, 0, 0 };
  for (int i = 0; i < 3; i++) {
    modWrite(addr, on, 3);
    delay(80);
    modWrite(addr, off, 3);
    delay(80);
  }
}

// ---------------------------------------------------------------------------
// Modulino Distance
// ---------------------------------------------------------------------------

bool tofReady() {
  return tofCD || tofED;
}

void tofStart() {
  tofCD = new VL53L4CD(&Wire1, -1);
  if (tofCD->InitSensor() == VL53L4CD_ERROR_NONE) {
    tofCD->VL53L4CD_SetRangeTiming(20, 0);
    tofCD->VL53L4CD_StartRanging();
    return;
  }
  delete tofCD;
  tofCD = nullptr;
  tofED = new VL53L4ED(&Wire1, -1);
  if (tofED->InitSensor() == VL53L4ED_ERROR_NONE) {
    tofED->VL53L4ED_SetRangeTiming(20, 0);
    tofED->VL53L4ED_StartRanging();
    return;
  }
  delete tofED;
  tofED = nullptr;
}

void tofStop() {
  delete tofCD;
  delete tofED;
  tofCD = nullptr;
  tofED = nullptr;
  handMm = -1;
  handReported = -2;
}

void pollTof(uint32_t now) {
  if (!tofReady() || now - lastTofPoll < TOF_POLL_MS) return;
  lastTofPoll = now;
  uint8_t ready = 0;
  if (tofCD) {
    tofCD->VL53L4CD_CheckForDataReady(&ready);
    if (ready) {
      VL53L4CD_Result_t r;
      tofCD->VL53L4CD_ClearInterrupt();
      tofCD->VL53L4CD_GetResult(&r);
      handMm = r.range_status == 0 ? r.distance_mm : -1;
    }
  } else {
    tofED->VL53L4ED_CheckForDataReady(&ready);
    if (ready) {
      VL53L4ED_ResultsData_t r;
      tofED->VL53L4ED_ClearInterrupt();
      tofED->VL53L4ED_GetResult(&r);
      handMm = r.range_status == 0 ? r.distance_mm : -1;
    }
  }
  bool changed = abs(handMm - handReported) >= 3 || (handMm < 0) != (handReported < 0);
  if ((changed && now - lastTofReport >= TOF_REPORT_MIN_MS) || now - lastTofReport >= TOF_REPORT_MAX_MS) {
    Bridge.notify("hand", (int)handMm);
    handReported = handMm;
    lastTofReport = now;
  }
}

bool isReserved(uint8_t a) {
  for (uint8_t r : RESERVED_ADDRS)
    if (r == a) return true;
  return false;
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

int findModule(uint8_t addr) {
  for (int i = 0; i < moduleCount; i++)
    if (modules[i].addr == addr) return i;
  return -1;
}

void addModule(uint8_t addr) {
  if (moduleCount >= MAX_MODULES) return;
  ButtonsModule& m = modules[moduleCount++];
  memset(&m, 0, sizeof(m));
  m.addr = addr;
  m.ledMask = 0xFF;
  // keep sorted by address: pad numbering = address order = plug-in order
  for (int i = moduleCount - 1; i > 0 && modules[i].addr < modules[i - 1].addr; i--) {
    ButtonsModule t = modules[i];
    modules[i] = modules[i - 1];
    modules[i - 1] = t;
  }
  topologyDirty = true;
}

uint8_t nextFreeButtonsAddr() {
  for (uint8_t a = BUTTONS_FIRST_ADDR; a <= BUTTONS_LAST_ADDR; a++) {
    if (isReserved(a) || a == knobAddr) continue;
    if (findModule(a) >= 0 || probe(a)) continue;
    return a;
  }
  return 0;
}

void scanBus() {
  bool seen[MAX_MODULES] = { false };
  bool knobSeen = false;
  bool buzzerSeen = false;
  bool tofSeen = false;
  bool defaultSeen = false;

  for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
    if (!probe(addr)) continue;
    if (addr == TOF_ADDR) {  // no pinstrap byte to read; don't disturb its register pointer
      tofSeen = true;
      continue;
    }
    uint8_t pin = 0, scratch[3];
    if (!modRead(addr, scratch, 1, &pin)) continue;

    if (pin == PINSTRAP_BUTTONS) {
      uint8_t a = addr;
      if (a == BUTTONS_DEFAULT_ADDR) {
        defaultSeen = true;
        if (enrolHold) continue;
        // Fresh module: move it out of the way so the next one can be plugged in.
        uint8_t target = nextFreeButtonsAddr();
        if (target && changeAddress(a, target)) {
          a = target;
          flashModule(a);
        }
      }
      int idx = findModule(a);
      if (idx < 0) {
        addModule(a);
        idx = findModule(a);
      }
      if (idx >= 0) seen[idx] = true;
    } else if (pin == PINSTRAP_BUZZER) {
      buzzerSeen = true;
      if (buzzerAddr != addr) {
        buzzerAddr = addr;
        topologyDirty = true;
      }
    } else if (pin == PINSTRAP_KNOB_A || pin == PINSTRAP_KNOB_B) {
      knobSeen = true;
      if (knobAddr != addr) {
        knobAddr = addr;
        uint8_t b[3];
        if (modRead(knobAddr, b, 3)) knobLast = (int16_t)(b[0] | (b[1] << 8));
        topologyDirty = true;
      }
    }
  }

  // drop modules that disappeared (indices shift, so walk backwards)
  for (int i = moduleCount - 1; i >= 0; i--) {
    if (seen[i]) continue;
    for (int j = i; j < moduleCount - 1; j++) modules[j] = modules[j + 1];
    moduleCount--;
    topologyDirty = true;
  }
  if (!defaultSeen) enrolHold = false;
  if (tofSeen && !tofReady()) {
    tofStart();
    if (tofReady()) topologyDirty = true;
  } else if (!tofSeen && tofReady()) {
    tofStop();
    topologyDirty = true;
  }
  if (!buzzerSeen && buzzerAddr) {
    buzzerAddr = 0;
    topologyDirty = true;
  }
  if (!knobSeen && knobAddr) {
    knobAddr = 0;
    topologyDirty = true;
  }
}

void reportTopology() {
  // "3e,40,41" style list helps debugging from the web UI
  String addrs;
  for (int i = 0; i < moduleCount; i++) {
    if (i) addrs += ",";
    addrs += String(modules[i].addr, HEX);
  }
  Bridge.notify("topology", moduleCount, knobAddr != 0, addrs, buzzerAddr != 0, tofReady());
  for (int i = 0; i < moduleCount; i++) modules[i].ledMask = 0xFF;  // force LED refresh
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

void pollButtons(uint32_t now) {
  for (int mi = 0; mi < moduleCount; mi++) {
    ButtonsModule& m = modules[mi];
    uint8_t b[3];
    if (!modRead(m.addr, b, 3)) continue;
    for (int k = 0; k < PADS_PER_MODULE; k++) {
      int pad = mi * PADS_PER_MODULE + k;
      bool pressed = b[k] != 0;
      if (pressed != m.down[k] && now - m.changedAt[k] >= DEBOUNCE_MS) {
        m.down[k] = pressed;
        m.changedAt[k] = now;
        if (pressed) {
          m.downAt[k] = now;
          m.holdSent[k] = false;
          Bridge.notify("pad_down", pad);
        } else {
          Bridge.notify("pad_up", pad, (int)(now - m.downAt[k]));
        }
      }
      if (m.down[k] && !m.holdSent[k] && now - m.downAt[k] >= holdMs) {
        m.holdSent[k] = true;
        Bridge.notify("pad_hold", pad);
      }
    }
  }
}

void pollKnob(uint32_t now) {
  if (!knobAddr) return;
  uint8_t b[3];
  if (!modRead(knobAddr, b, 3)) return;
  int16_t pos = (int16_t)(b[0] | (b[1] << 8));
  bool pressed = b[2] != 0;

  knobAccum += (int16_t)(pos - knobLast);  // int16 wrap-safe
  knobLast = pos;
  if (knobAccum != 0 && now - lastKnobReport >= KNOB_REPORT_MS) {
    Bridge.notify("knob_turn", (int)knobAccum);
    knobAccum = 0;
    lastKnobReport = now;
  }

  if (pressed && !knobDown) {
    knobDown = true;
    knobDownAt = now;
    knobLongSent = false;
  } else if (!pressed && knobDown) {
    knobDown = false;
    if (!knobLongSent) Bridge.notify("knob_click");
  }
  if (knobDown && !knobLongSent && now - knobDownAt >= KNOB_LONG_MS) {
    knobLongSent = true;
    Bridge.notify("knob_long");
  }
}

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

bool ledFor(uint8_t state, uint32_t now) {
  switch (state) {
    case PAD_LOADED: return true;
    case PAD_PLAYING: return (now / 60) % 2;      // flicker while sounding
    case PAD_RECORDING: return (now / 120) % 2;   // fast blink
    case PAD_ARMED: return (now / 300) % 2;       // slow blink
    default: return false;
  }
}

void refreshLeds(uint32_t now) {
  for (int mi = 0; mi < moduleCount; mi++) {
    ButtonsModule& m = modules[mi];
    uint8_t mask = 0;
    for (int k = 0; k < PADS_PER_MODULE; k++)
      if (ledFor(padStates[mi * PADS_PER_MODULE + k], now)) mask |= 1 << k;
    if (mask == m.ledMask) continue;
    uint8_t buf[3] = { (uint8_t)(mask & 1), (uint8_t)((mask >> 1) & 1), (uint8_t)((mask >> 2) & 1) };
    modWrite(m.addr, buf, 3);
    m.ledMask = mask;
  }
}

// ---------------------------------------------------------------------------
// Bridge API (called from Linux)
// ---------------------------------------------------------------------------

// One byte per pad, PadState values.
void setStates(std::vector<uint8_t> states) {
  size_t n = min(states.size(), (size_t)MAX_PADS);
  for (size_t i = 0; i < n; i++) padStates[i] = states[i];
  for (size_t i = n; i < MAX_PADS; i++) padStates[i] = PAD_EMPTY;
}

void setHoldMs(int ms) {
  holdMs = constrain(ms, 200, 3000);
}

// 8x13 grayscale frame, values 0..7
void drawFrame(std::vector<uint8_t> frame) {
  if (frame.size() < sizeof(pendingFrame)) return;
  k_mutex_lock(&frameMutex, K_FOREVER);
  memcpy(pendingFrame, frame.data(), sizeof(pendingFrame));
  framePending = true;
  k_mutex_unlock(&frameMutex);
}

// Modulino Buzzer: piezo tone generator, frequency + duration only (0 Hz = silence)
void buzz(int freq, int ms) {
  if (!buzzerAddr) return;
  uint32_t f = max(freq, 0), d = max(ms, 0);
  uint8_t buf[8];
  memcpy(&buf[0], &f, 4);
  memcpy(&buf[4], &d, 4);
  modWrite(buzzerAddr, buf, 8);
}

void rescan() {
  topologyDirty = true;
  lastScan = 0;
}

// Move every Buttons module back to the factory address so they can be
// re-enrolled one at a time (unplug all, plug back in the order you want).
void resetAddresses() {
  for (int i = 0; i < moduleCount; i++) changeAddress(modules[i].addr, 0);
  moduleCount = 0;
  enrolHold = true;
  topologyDirty = true;
}

// ---------------------------------------------------------------------------

void setup() {
  k_mutex_init(&frameMutex);
  matrix.begin();
  matrix.setGrayscaleBits(3);
  matrix.clear();

  Wire1.begin();
  Wire1.setClock(400000);

  Bridge.begin();
  // _safe variants run inside loop(), so they never race the I2C polling
  Bridge.provide_safe("set_states", setStates);
  Bridge.provide_safe("set_hold_ms", setHoldMs);
  Bridge.provide_safe("rescan", rescan);
  Bridge.provide_safe("buzz", buzz);
  Bridge.provide_safe("reset_addresses", resetAddresses);
  Bridge.provide("draw", drawFrame);

  memset(padStates, PAD_EMPTY, sizeof(padStates));
}

void loop() {
  uint32_t now = millis();

  if (lastScan == 0 || now - lastScan >= SCAN_INTERVAL_MS) {
    lastScan = now ? now : 1;
    scanBus();
  }
  if (topologyDirty) {
    topologyDirty = false;
    reportTopology();
  }

  if (now - lastPoll >= POLL_INTERVAL_MS) {
    lastPoll = now;
    pollButtons(now);
    pollKnob(now);
    pollTof(now);
    refreshLeds(now);
  }

  k_mutex_lock(&frameMutex, K_FOREVER);
  if (framePending) {
    matrix.draw(pendingFrame);
    framePending = false;
  }
  k_mutex_unlock(&frameMutex);
}
