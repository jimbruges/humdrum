// Humdrum — MCU side (STM32U585)
//
// Owns everything on the Qwiic bus and the on-board LED matrix:
//   * discovers any number of Modulino Buttons (3 pads each), up to 4 Modulino Knobs,
//     up to 4 Modulino Pixels (used as level meters),
//     an optional Modulino Buzzer (a tone-only "speaker" for testing) and an
//     optional Modulino Distance (hand height for the theremin effect)
//   * gives each newly plugged Buttons, Knob or Pixels module its own I2C address automatically
//   * reports pad / knob events to Linux over the Bridge
//   * shows pad states on the button LEDs and frames sent by Linux on the matrix
//
// Linux (python/main.py) is the source of truth for what is loaded where; this
// sketch only reports input and renders what it is told.

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include <Wire.h>
#include <vector>
#include <stm32u5xx.h>

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
static const uint8_t PINSTRAP_PIXELS = 0x6C;
// The Distance module is a bare ST sensor, not a Modulino MCU: fixed address and
// no pinstrap byte, so it's handled separately from the scan.
static const uint8_t TOF_ADDR = 0x29;

static const uint8_t BUTTONS_DEFAULT_ADDR = 0x3E;  // factory address
// New Buttons modules are moved into this range, in plug-in order, so pad
// numbering follows the order you add modules.
static const uint8_t BUTTONS_FIRST_ADDR = 0x40;
static const uint8_t BUTTONS_LAST_ADDR = 0x5F;
static const uint8_t RESERVED_ADDRS[] = { 0x44 };  // Modulino Thermo is fixed here

// Knobs ship on 0x3A (or 0x3B with the solder jumper); new ones are moved here,
// in plug-in order, so several can share the bus. Knob 1 is the first plugged.
static const uint8_t KNOB_DEFAULT_ADDRS[] = { 0x3A, 0x3B };
static const uint8_t KNOB_FIRST_ADDR = 0x60;
static const uint8_t KNOB_LAST_ADDR = 0x67;
static const int MAX_KNOBS = 4;
// Pixels ship on 0x36 and are moved here in plug-in order.
static const uint8_t PIXELS_DEFAULT_ADDR = 0x36;
static const uint8_t PIXELS_FIRST_ADDR = 0x70;
static const uint8_t PIXELS_LAST_ADDR = 0x77;
static const int MAX_PIXELS = 4;
static const int PIXELS_LEDS = 8;
static const uint8_t PIXELS_BRIGHTNESS = 2;  // of 31: each lit LED draws little; the chain shares one 3.3 V supply

static const int MAX_MODULES = 16;  // 48 pads
static const int PADS_PER_MODULE = 3;
static const int MAX_PADS = MAX_MODULES * PADS_PER_MODULE;

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------
// 100 kHz: a long chain of Modulinos and Qwiic cables is too much load for 400 kHz
static const uint32_t BUS_HZ = 100000;
static const uint32_t SCAN_INTERVAL_MS = 1000;
static const uint32_t POLL_INTERVAL_MS = 10;  // keeps the bus mostly idle; still under the 12 ms debounce
static const uint32_t KNOB_REPORT_MS = 15;
static const uint32_t KNOB_LONG_MS = 700;
static const uint32_t DEBOUNCE_MS = 12;
// The knob's switch must read the same this many polls in a row (~12 ms) to count
static const uint8_t KNOB_STABLE_READS = 3;
// More detent steps than this in one 4 ms poll can only be a corrupted read
static const int16_t KNOB_MAX_STEP = 40;
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

struct KnobModule {
  uint8_t addr;
  int16_t last;
  int32_t accum;
  uint32_t lastReport;
  bool down;
  bool rawLast;
  uint8_t rawRun;
  uint32_t downAt;
  bool longSent;
};
KnobModule knobs[MAX_KNOBS];
int knobCount = 0;

struct PixelsModule {
  uint8_t addr;
  uint8_t level;  // 0..255 from Linux
  uint8_t tint;   // 0..255: how much effect is on what it meters (green -> blue)
  uint8_t shown;  // LEDs lit last time we wrote, 0xFF = unknown
  uint8_t shownTint;
};
PixelsModule pixels[MAX_PIXELS];
int pixelsCount = 0;
String busReport;  // "40:7c,60:74,..." everything that answered in the last scan
bool busReported = false;  // so an empty bus gets reported too
uint8_t buzzerAddr = 0;

// Why the Distance sensor didn't start, for the bus report: 1 = no reply to
// the ID read, 2 = wrong ID (tofId), 3 = the sensor's own setup failed
int tofErr = 0;
uint16_t tofId = 0;
uint32_t tofRetryAt = 0;
static const uint32_t TOF_RETRY_MS = 10000;  // a failed start blocks for up to 2 s
VL53L4CD* tofCD = nullptr;  // whichever variant answered
VL53L4ED* tofED = nullptr;
int16_t handMm = -1;  // -1: nothing in range
int16_t handReported = -2;
uint32_t lastTofPoll = 0;
uint32_t lastTofReport = 0;
uint32_t badReads = 0;  // Modulino reads rejected as corrupted

uint8_t padStates[MAX_PADS];
uint32_t holdMs = 600;

uint32_t lastScan = 0;
uint32_t lastPoll = 0;
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

// Free the bus when a module is holding it. If the MCU restarts halfway through
// a read, a module can sit holding SDA low waiting for clocks that never come,
// and then nothing in the chain answers until it is unplugged. Clocking SCL by
// hand until it lets go, then sending a STOP, frees it. Qwiic: PD12 SCL, PD13 SDA.
bool recoverBus() {
  static const uint32_t SCL_PIN = 12, SDA_PIN = 13;
  GPIO_TypeDef* g = GPIOD;
  RCC->AHB2ENR1 |= RCC_AHB2ENR1_GPIODEN;
  if (g->IDR & (1u << SDA_PIN)) return false;  // not stuck
  uint32_t moder = g->MODER, otyper = g->OTYPER;
  g->BSRR = (1u << SCL_PIN) | (1u << SDA_PIN);  // both released
  g->OTYPER |= (1u << SCL_PIN) | (1u << SDA_PIN);  // open drain
  g->MODER = (g->MODER & ~((3u << SCL_PIN * 2) | (3u << SDA_PIN * 2))) | (1u << SCL_PIN * 2) | (1u << SDA_PIN * 2);
  for (int i = 0; i < 18 && !(g->IDR & (1u << SDA_PIN)); i++) {
    g->BRR = 1u << SCL_PIN;
    delayMicroseconds(5);
    g->BSRR = 1u << SCL_PIN;
    delayMicroseconds(5);
  }
  g->BRR = 1u << SDA_PIN;  // STOP: SDA rises while SCL is high
  delayMicroseconds(5);
  g->BSRR = 1u << SDA_PIN;
  delayMicroseconds(5);
  g->OTYPER = otyper;
  g->MODER = moder;
  if (I2C4->CR1 & I2C_CR1_PE) {  // and reset the I2C peripheral's own state
    I2C4->CR1 &= ~I2C_CR1_PE;
    delayMicroseconds(5);
    I2C4->CR1 |= I2C_CR1_PE;
  }
  return true;
}

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

void flashModule(uint8_t addr);

// Same protocol as Arduino's Modulino AddressChanger. The module stores the new
// address in flash, but only uses it after a power cycle (it goes quiet until
// then, and NACKs the end of the command as it restarts), so this just sends it.
// The user then unplugs and replugs that one module to finish.
void sendAddress(uint8_t from, uint8_t to) {
  uint8_t data[40] = { 'C', 'F', (uint8_t)(to * 2) };  // to = 0: back to the factory address
  Wire1.beginTransmission(from);
  Wire1.write(data, sizeof(data));
  Wire1.endTransmission();
  delay(50);
}

// One new module is set up at a time: it gets an address, then waits for its replug.
uint8_t pendingTo = 0;    // address the new module will come back on, 0 = none
uint8_t pendingPin = 0;   // its pinstrap, to recognise it there
uint32_t pendingSince = 0;
// Off while modules are being set up one at a time from the web UI: then
// nothing moves on its own and Linux chooses each address (see setAddress()).
bool autoEnrol = true;
static const uint32_t ENROL_RESEND_MS = 20000;  // still on the factory address: send again

// States reported to Linux with notify("enrol", addr, state)
enum EnrolState { ENROL_REPLUG = 1, ENROL_DONE = 2, ENROL_RESET = 3 };

void enrol(uint8_t from, uint8_t pin, uint8_t target) {
  if (!target) return;
  uint32_t now = millis();
  if (pendingTo && now - pendingSince < ENROL_RESEND_MS) return;  // waiting for the replug
  sendAddress(from, target);
  pendingTo = target;
  pendingPin = pin;
  pendingSince = now;
  Bridge.notify("enrol", (int)target, (int)ENROL_REPLUG);
}

// Called for every module that answers on its own (non-factory) address.
void checkEnrolled(uint8_t addr, uint8_t pin) {
  if (!pendingTo || addr != pendingTo || pin != pendingPin) return;
  pendingTo = 0;
  Bridge.notify("enrol", (int)addr, (int)ENROL_DONE);
  if (pin == PINSTRAP_BUTTONS) flashModule(addr);
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
  tofId = 0;
  if (tofCD->VL53L4CD_GetSensorId(&tofId) != VL53L4CD_ERROR_NONE) {
    tofErr = 1;
    delete tofCD;
    tofCD = nullptr;
    return;
  }
  tofErr = tofId == 0xEBAA ? 3 : 2;
  if (tofCD->InitSensor() == VL53L4CD_ERROR_NONE) {
    tofErr = 0;
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
    tofErr = 0;
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
    if (isReserved(a)) continue;
    if (findModule(a) >= 0 || probe(a)) continue;
    return a;
  }
  return 0;
}

int findKnob(uint8_t addr) {
  for (int i = 0; i < knobCount; i++)
    if (knobs[i].addr == addr) return i;
  return -1;
}

void addKnob(uint8_t addr) {
  if (knobCount >= MAX_KNOBS) return;
  KnobModule& k = knobs[knobCount++];
  memset(&k, 0, sizeof(k));
  k.addr = addr;
  uint8_t b[3];
  if (modRead(addr, b, 3)) k.last = (int16_t)(b[0] | (b[1] << 8));
  for (int i = knobCount - 1; i > 0 && knobs[i].addr < knobs[i - 1].addr; i--) {
    KnobModule t = knobs[i];
    knobs[i] = knobs[i - 1];
    knobs[i - 1] = t;
  }
  topologyDirty = true;
}

bool isKnobDefault(uint8_t a) {
  for (uint8_t d : KNOB_DEFAULT_ADDRS)
    if (d == a) return true;
  return false;
}

uint8_t nextFreeKnobAddr() {
  for (uint8_t a = KNOB_FIRST_ADDR; a <= KNOB_LAST_ADDR; a++)
    if (findKnob(a) < 0 && !probe(a)) return a;
  return 0;
}

int findPixels(uint8_t addr) {
  for (int i = 0; i < pixelsCount; i++)
    if (pixels[i].addr == addr) return i;
  return -1;
}

void addPixels(uint8_t addr) {
  if (pixelsCount >= MAX_PIXELS) return;
  PixelsModule& px = pixels[pixelsCount++];
  px.addr = addr;
  px.level = 0;
  px.tint = 0;
  px.shown = 0xFF;
  for (int i = pixelsCount - 1; i > 0 && pixels[i].addr < pixels[i - 1].addr; i--) {
    PixelsModule t = pixels[i];
    pixels[i] = pixels[i - 1];
    pixels[i - 1] = t;
  }
  topologyDirty = true;
}

uint8_t nextFreePixelsAddr() {
  for (uint8_t a = PIXELS_FIRST_ADDR; a <= PIXELS_LAST_ADDR; a++)
    if (findPixels(a) < 0 && !probe(a)) return a;
  return 0;
}

void scanBus() {
  bool seen[MAX_MODULES] = { false };
  bool knobSeen[MAX_KNOBS] = { false };
  bool pixelsSeen[MAX_PIXELS] = { false };
  String report;
  bool buzzerSeen = false;
  bool tofSeen = false;
  bool defaultSeen = false;

  for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
    if (!probe(addr)) continue;
    if (addr == TOF_ADDR) {  // no pinstrap byte to read; don't disturb its register pointer
      tofSeen = true;
      if (report.length()) report += ",";
      report += String(addr, HEX) + ":tof";
      if (!tofReady() && tofErr) report += "!" + String(tofErr) + "-" + String(tofId, HEX);
      continue;
    }
    uint8_t pin = 0, scratch[3];
    bool ok = modRead(addr, scratch, 1, &pin);
    if (report.length()) report += ",";
    report += String(addr, HEX) + ":" + (ok ? String(pin, HEX) : String("?"));
    if (!ok) continue;

    if (pin == PINSTRAP_BUTTONS) {
      uint8_t a = addr;
      if (a == BUTTONS_DEFAULT_ADDR) {
        // Fresh module: give it the next free address, then it needs a replug.
        defaultSeen = true;
        if (!enrolHold && autoEnrol) enrol(a, pin, nextFreeButtonsAddr());
        continue;
      }
      if (a < BUTTONS_FIRST_ADDR || a > BUTTONS_LAST_ADDR) continue;  // stray: fix it on the modules page
      checkEnrolled(a, pin);
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
    } else if (pin == PINSTRAP_PIXELS) {
      uint8_t a = addr;
      if (a == PIXELS_DEFAULT_ADDR) {
        defaultSeen = true;
        if (!enrolHold && autoEnrol) enrol(a, pin, nextFreePixelsAddr());
        continue;
      }
      if (a < PIXELS_FIRST_ADDR || a > PIXELS_LAST_ADDR) continue;
      checkEnrolled(a, pin);
      int idx = findPixels(a);
      if (idx < 0) {
        addPixels(a);
        idx = findPixels(a);
      }
      if (idx >= 0) pixelsSeen[idx] = true;
    } else if (pin == PINSTRAP_KNOB_A || pin == PINSTRAP_KNOB_B) {
      uint8_t a = addr;
      if (isKnobDefault(a)) {
        defaultSeen = true;
        if (!enrolHold && autoEnrol) enrol(a, pin, nextFreeKnobAddr());
        continue;
      }
      if (a < KNOB_FIRST_ADDR || a > KNOB_LAST_ADDR) continue;
      checkEnrolled(a, pin);
      int idx = findKnob(a);
      if (idx < 0) {
        addKnob(a);
        idx = findKnob(a);
      }
      if (idx >= 0) knobSeen[idx] = true;
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
  if (tofSeen && !tofReady() && (int32_t)(millis() - tofRetryAt) >= 0) {
    tofStart();
    tofRetryAt = millis() + TOF_RETRY_MS;
    if (tofReady()) topologyDirty = true;
  } else if (!tofSeen && tofReady()) {
    tofStop();
    topologyDirty = true;
  }
  if (!buzzerSeen && buzzerAddr) {
    buzzerAddr = 0;
    topologyDirty = true;
  }
  for (int i = pixelsCount - 1; i >= 0; i--) {
    if (pixelsSeen[i]) continue;
    for (int j = i; j < pixelsCount - 1; j++) pixels[j] = pixels[j + 1];
    pixelsCount--;
    topologyDirty = true;
  }
  if (!report.length()) {
    if (recoverBus()) report = "stuck";  // freed: the next scan sees the modules
    else report = "lines:" + String((GPIOD->IDR >> 12) & 3) + ":" + String(GPIOD->MODER >> 24, HEX) + ":" + String(I2C4->ISR, HEX);
  }
  if (report != busReport || !busReported) {
    busReport = report;
    busReported = true;
    Bridge.notify("bus", busReport);
  }
  for (int i = knobCount - 1; i >= 0; i--) {
    if (knobSeen[i]) continue;
    for (int j = i; j < knobCount - 1; j++) knobs[j] = knobs[j + 1];
    knobCount--;
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
  String knobAddrs;
  for (int i = 0; i < knobCount; i++) {
    if (i) knobAddrs += ",";
    knobAddrs += String(knobs[i].addr, HEX);
  }
  String pixelsAddrs;
  for (int i = 0; i < pixelsCount; i++) {
    if (i) pixelsAddrs += ",";
    pixelsAddrs += String(pixels[i].addr, HEX);
    pixels[i].shown = 0xFF;  // force a redraw
  }
  // flags: 1 = buzzer, 2 = distance. Six arguments at most: more don't arrive.
  int flags = (buzzerAddr ? 1 : 0) | (tofReady() ? 2 : 0);
  Bridge.notify("topology", moduleCount, knobCount, addrs, flags, knobAddrs, pixelsAddrs);
  for (int i = 0; i < moduleCount; i++) modules[i].ledMask = 0xFF;  // force LED refresh
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

// A read is only trusted if the module answers with its own pinstrap byte and
// the payload makes sense. A glitched transfer (e.g. all 0xFF) would otherwise
// look like a press, and a phantom knob click changes the effect.
bool buttonsReadOk(uint8_t pin, const uint8_t* b) {
  return pin == PINSTRAP_BUTTONS && b[0] <= 1 && b[1] <= 1 && b[2] <= 1;
}

bool knobReadOk(uint8_t pin, const uint8_t* b) {
  return (pin == PINSTRAP_KNOB_A || pin == PINSTRAP_KNOB_B) && b[2] <= 1;
}

void pollButtons(uint32_t now) {
  for (int mi = 0; mi < moduleCount; mi++) {
    ButtonsModule& m = modules[mi];
    uint8_t b[3], pin = 0;
    if (!modRead(m.addr, b, 3, &pin)) continue;
    if (!buttonsReadOk(pin, b)) {
      badReads++;
      continue;
    }
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

void pollKnob(int ki, uint32_t now) {
  KnobModule& k = knobs[ki];
  uint8_t b[3], pin = 0;
  if (!modRead(k.addr, b, 3, &pin)) return;
  if (!knobReadOk(pin, b)) {
    badReads++;
    return;
  }
  int16_t pos = (int16_t)(b[0] | (b[1] << 8));
  int16_t step = (int16_t)(pos - k.last);  // int16 wrap-safe
  if (abs(step) > KNOB_MAX_STEP) {
    badReads++;
    return;
  }
  k.accum += step;
  k.last = pos;

  bool raw = b[2] != 0;
  if (raw == k.rawLast) {
    if (k.rawRun < 255) k.rawRun++;
  } else {
    k.rawLast = raw;
    k.rawRun = 1;
  }
  bool pressed = k.rawRun >= KNOB_STABLE_READS ? raw : k.down;
  if (k.accum != 0 && now - k.lastReport >= KNOB_REPORT_MS) {
    Bridge.notify("knob_turn", ki, (int)k.accum);
    k.accum = 0;
    k.lastReport = now;
  }

  if (pressed && !k.down) {
    k.down = true;
    k.downAt = now;
    k.longSent = false;
  } else if (!pressed && k.down) {
    k.down = false;
    if (!k.longSent) Bridge.notify("knob_click", ki);
  }
  if (k.down && !k.longSent && now - k.downAt >= KNOB_LONG_MS) {
    k.longSent = true;
    Bridge.notify("knob_long", ki);
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

// Level meter: green, then amber, then red at the top. Frames are 4 bytes per
// LED (0xE0 | brightness, blue, green, red), as the Modulino library writes them.
void refreshPixels() {
  for (int i = 0; i < pixelsCount; i++) {
    PixelsModule& px = pixels[i];
    uint8_t lit = (px.level * PIXELS_LEDS + 127) / 255;
    if (lit == px.shown && px.tint == px.shownTint) continue;
    uint8_t buf[PIXELS_LEDS * 4];
    for (int l = 0; l < PIXELS_LEDS; l++) {
      uint8_t r = 0, g = 0, b = 0;
      if (l < lit) {
        if (l < 5) {  // the effect amount shifts the safe zone from green to blue
          g = 255 - (px.tint * 195) / 255;
          b = px.tint;
        }
        else if (l < 7) { r = 255; g = 120; }
        else r = 255;
      }
      buf[l * 4] = 0xE0 | PIXELS_BRIGHTNESS;
      buf[l * 4 + 1] = b;
      buf[l * 4 + 2] = g;
      buf[l * 4 + 3] = r;
    }
    modWrite(px.addr, buf, sizeof(buf));
    px.shown = lit;
    px.shownTint = px.tint;
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
// From the modules page: show which module is which. Buttons flash their LEDs,
// Pixels flash blue; both go back to what they were showing afterwards.
// Works on any module that answers, set up or not (e.g. on its factory address).
void identify(int addr) {
  uint8_t pin = 0, scratch[1];
  if (!modRead(addr, scratch, 1, &pin)) return;
  if (pin == PINSTRAP_BUTTONS) {
    flashModule(addr);
    int m = findModule(addr);
    if (m >= 0) modules[m].ledMask = 0xFF;  // redraw the pad lights
    return;
  }
  if (pin != PINSTRAP_PIXELS) return;
  int p = findPixels(addr);
  uint8_t buf[PIXELS_LEDS * 4];
  for (int i = 0; i < 6; i++) {
    for (int l = 0; l < PIXELS_LEDS; l++) {
      // blue on every other LED, at meter brightness: no current spike
      bool on = i % 2 == 0 && l % 2 == 0;
      buf[l * 4] = 0xE0 | PIXELS_BRIGHTNESS;
      buf[l * 4 + 1] = on ? 255 : 0;
      buf[l * 4 + 2] = buf[l * 4 + 3] = 0;
    }
    modWrite(addr, buf, sizeof(buf));
    delay(150);
  }
  if (p >= 0) pixels[p].shown = 0xFF;  // redraw the meter
}

void setStates(std::vector<uint8_t> states) {
  size_t n = min(states.size(), (size_t)MAX_PADS);
  for (size_t i = 0; i < n; i++) padStates[i] = states[i];
  for (size_t i = n; i < MAX_PADS; i++) padStates[i] = PAD_EMPTY;
}

// One byte per Pixels module (address order), 0..255 = how full the meter is,
// then optionally one more each: how much effect is on (tints the meter).
void setMeters(std::vector<uint8_t> levels) {
  int n = levels.size();
  for (int i = 0; i < pixelsCount && i < n; i++) {
    pixels[i].level = levels[i];
    if (pixelsCount + i < n) pixels[i].tint = levels[pixelsCount + i];
  }
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
  busReported = false;  // send it again too
  lastScan = 0;
}

// From the web UI's module setup: move the module at `from` to `to` (0 = back
// to its factory address). It takes effect when that module is replugged.
void setAddress(int from, int to) {
  uint8_t pin = 0, scratch[1];
  bool known = modRead(from, scratch, 1, &pin);
  sendAddress(from, to);
  if (to && known) {
    pendingTo = to;
    pendingPin = pin;
    pendingSince = millis();
    Bridge.notify("enrol", to, (int)ENROL_REPLUG);
  }
  lastScan = 0;  // rescan now
}

void setAutoEnrol(bool on) {
  if (on == autoEnrol) return;  // Linux resends it every few seconds
  autoEnrol = on;
  pendingTo = 0;
}

// Send every Modulino back to its factory address (Arduino's broadcast reset,
// I2C general call 0x00), so they can be set up again one at a time: unplug
// them all, then plug them back in, in the order you want them numbered.
void resetAddresses() {
  sendAddress(0x00, 0);
  pendingTo = 0;
  moduleCount = 0;
  knobCount = 0;
  pixelsCount = 0;
  enrolHold = true;
  topologyDirty = true;
  Bridge.notify("enrol", 0, (int)ENROL_RESET);
}

// ---------------------------------------------------------------------------

void setup() {
  k_mutex_init(&frameMutex);
  matrix.begin();
  matrix.setGrayscaleBits(3);
  matrix.clear();

  recoverBus();
  Wire1.begin();
  Wire1.setClock(BUS_HZ);

  Bridge.begin();
  // _safe variants run inside loop(), so they never race the I2C polling
  Bridge.provide_safe("set_states", setStates);
  Bridge.provide_safe("set_hold_ms", setHoldMs);
  Bridge.provide_safe("rescan", rescan);
  Bridge.provide_safe("buzz", buzz);
  Bridge.provide_safe("set_meters", setMeters);
  Bridge.provide_safe("reset_addresses", resetAddresses);
  Bridge.provide_safe("set_address", setAddress);
  Bridge.provide_safe("identify", identify);
  Bridge.provide_safe("set_auto_enrol", setAutoEnrol);
  Bridge.provide("draw", drawFrame);

  memset(padStates, PAD_EMPTY, sizeof(padStates));
}

void loop() {
  uint32_t now = millis();

  if (lastScan == 0 || now - lastScan >= SCAN_INTERVAL_MS) {
    lastScan = now ? now : 1;
    scanBus();
    static uint32_t badReported = 0;
    if (badReads != badReported) {
      badReported = badReads;
      Bridge.notify("bad_reads", (int)badReads);
    }
  }
  if (topologyDirty) {
    topologyDirty = false;
    reportTopology();
  }

  if (now - lastPoll >= POLL_INTERVAL_MS) {
    lastPoll = now;
    pollButtons(now);
    for (int ki = 0; ki < knobCount; ki++) pollKnob(ki, now);
    pollTof(now);
    refreshLeds(now);
    refreshPixels();
  }

  k_mutex_lock(&frameMutex, K_FOREVER);
  if (framePending) {
    matrix.draw(pendingFrame);
    framePending = false;
  }
  k_mutex_unlock(&frameMutex);
}
