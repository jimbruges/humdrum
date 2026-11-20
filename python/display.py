"""Frames for the UNO Q's 8x13 LED matrix, visible through the lid window.

The home screen is two channel strips that match the two knobs: the left half
is the phone (Bluetooth) and the sample pads, the right half is the mic.

    cols 0-4   Bluetooth symbol: dim = no phone, mid = connected, bright = music
               playing, struck through = muted, blinking = Bluetooth is off
    col  5     the Bluetooth effect's amount, bottom up (only while it's on)
    col  6     which slot the pads play: one dot, top = slot 1 ... row 6 = slot 4
               (bright while the Bluetooth knob is in slot mode)
    col  7     the mic effect's amount (only while it's on)
    cols 8-12  mic: bright = live, struck through = muted
    row  7     cols 0-5 phone + pads volume, cols 7-12 mic volume

Whatever a knob is set to change (its volume or its effect) is drawn brighter.

Anything you do shows a full-width banner for about a second: volume (icon,
0-99 and a bar), effect (its name and amount; a mark on row 6 says which side),
mute, knob mode, pad slot (S1-S4 and four marks), Bluetooth transport icons, connect / disconnect (then the
phone's name scrolls by) and a new track (its title scrolls by).
"""

from __future__ import annotations

from dataclasses import dataclass

ROWS, COLS = 8, 13
MAX = 7  # 3-bit grayscale
DIM = 2
ROTATE_180 = True  # the enclosure mounts the UNO Q rotated 180° (USB-C to the right wall)

# 3x5 glyphs, one string per row
FONT = {
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["##.", "..#", ".#.", "#..", "###"],
    "3": ["##.", "..#", ".#.", "..#", "##."],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "##.", "..#", "##."],
    "6": [".##", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "##."],
    "A": [".#.", "#.#", "###", "#.#", "#.#"],
    "B": ["##.", "#.#", "##.", "#.#", "##."],
    "C": [".##", "#..", "#..", "#..", ".##"],
    "D": ["##.", "#.#", "#.#", "#.#", "##."],
    "E": ["###", "#..", "##.", "#..", "###"],
    "F": ["###", "#..", "##.", "#..", "#.."],
    "G": [".##", "#..", "#.#", "#.#", ".##"],
    "H": ["#.#", "#.#", "###", "#.#", "#.#"],
    "I": ["###", ".#.", ".#.", ".#.", "###"],
    "J": ["..#", "..#", "..#", "#.#", ".#."],
    "K": ["#.#", "#.#", "##.", "#.#", "#.#"],
    "L": ["#..", "#..", "#..", "#..", "###"],
    "M": ["#.#", "###", "#.#", "#.#", "#.#"],
    "N": ["#.#", "###", "###", "#.#", "#.#"],
    "O": [".#.", "#.#", "#.#", "#.#", ".#."],
    "P": ["##.", "#.#", "##.", "#..", "#.."],
    "Q": [".#.", "#.#", "#.#", "##.", ".##"],
    "R": ["##.", "#.#", "##.", "#.#", "#.#"],
    "S": [".##", "#..", ".#.", "..#", "##."],
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "U": ["#.#", "#.#", "#.#", "#.#", "###"],
    "V": ["#.#", "#.#", "#.#", "#.#", ".#."],
    "W": ["#.#", "#.#", "###", "###", "#.#"],
    "X": ["#.#", "#.#", ".#.", "#.#", "#.#"],
    "Y": ["#.#", "#.#", ".#.", ".#.", ".#."],
    "Z": ["###", "..#", ".#.", "#..", "###"],
    "-": ["...", "...", "###", "...", "..."],
    "!": [".#.", ".#.", ".#.", "...", ".#."],
    "?": ["##.", "..#", ".#.", "...", ".#."],
    ".": ["...", "...", "...", "...", ".#."],
    ",": ["...", "...", "...", ".#.", "#.."],
    "'": [".#.", ".#.", "...", "...", "..."],
    ":": ["...", ".#.", "...", ".#.", "..."],
    "&": [".#.", "#.#", ".#.", "#.#", ".##"],
    "/": ["..#", "..#", ".#.", "#..", "#.."],
    "(": [".#.", "#..", "#..", "#..", ".#."],
    ")": [".#.", "..#", "..#", "..#", ".#."],
    " ": ["...", "...", "...", "...", "..."],
}

# Pixel-art icons, one string per row (any size up to 13x8)
ICONS = {
    "bt": [  # the Bluetooth symbol
        "..#..",
        "..##.",
        "#.#.#",
        ".###.",
        "#.#.#",
        "..##.",
        "..#..",
    ],
    "mic": [
        ".###.",
        ".###.",
        ".###.",
        "#.#.#",
        "#...#",
        ".###.",
        "..#..",
    ],
    "speaker": [
        "...#...",
        "..##..#",
        "####.#.",
        "####.#.",
        "####.#.",
        "..##..#",
        "...#...",
    ],
    "play": [
        "#...",
        "##..",
        "###.",
        "####",
        "###.",
        "##..",
        "#...",
    ],
    "pause": [
        "##.##",
        "##.##",
        "##.##",
        "##.##",
        "##.##",
        "##.##",
        "##.##",
    ],
    "next": [
        "#...#...#",
        "##..##..#",
        "###.###.#",
        "#########",
        "###.###.#",
        "##..##..#",
        "#...#...#",
    ],
    "prev": [
        "#...#...#",
        "#..##..##",
        "#.###.###",
        "#########",
        "#.###.###",
        "#..##..##",
        "#...#...#",
    ],
    "check": [
        ".....",
        "....#",
        "...##",
        "#.##.",
        "###..",
        ".#...",
        ".....",
    ],
    "cross": [
        ".....",
        "#...#",
        ".#.#.",
        "..#..",
        ".#.#.",
        "#...#",
        ".....",
    ],
    "bang": [
        ".#.",
        ".#.",
        ".#.",
        ".#.",
        "...",
        ".#.",
        "...",
    ],
    "note": [
        "..###",
        "..#.#",
        "..#.#",
        "..#..",
        ".##..",
        "###..",
        ".#...",
    ],
}


class Canvas:
    def __init__(self) -> None:
        self.px = [[0] * COLS for _ in range(ROWS)]

    def set(self, x: int, y: int, v: int) -> None:
        if 0 <= x < COLS and 0 <= y < ROWS:
            self.px[y][x] = max(0, min(MAX, int(v)))

    def icon(self, name: str, x: int, y: int, level: int = MAX) -> int:
        """Draw an icon with its top-left at (x, y); returns its width."""
        art = ICONS[name]
        for r, row in enumerate(art):
            for c, p in enumerate(row):
                if p == "#":
                    self.set(x + c, y + r, level)
        return len(art[0])

    def text(self, s: str, x: int | None = None, y: int = 0, level: int = MAX) -> None:
        """3x5 text, 4 columns per character; centred when x is None."""
        s = s.upper()
        if x is None:
            x = (COLS - (len(s) * 4 - 1)) // 2
        for i, ch in enumerate(s):
            for r, row in enumerate(FONT.get(ch, FONT["?"])):
                for c, p in enumerate(row):
                    if p == "#":
                        self.set(x + i * 4 + c, y + r, level)

    def hbar(self, x0: int, x1: int, y: int, value: float, level: int) -> None:
        """Bar from x0 to x1 inclusive, filled `value` (0..1) of the way, the last pixel partial."""
        lit = max(0.0, min(1.0, value)) * (x1 - x0 + 1)
        for i in range(x1 - x0 + 1):
            if i < int(lit):
                self.set(x0 + i, y, level)
            elif i < lit:
                self.set(x0 + i, y, max(1, round(level * (lit - i))))

    def vbar(self, x: int, y0: int, y1: int, value: float, level: int) -> None:
        """Bar in column x from y1 (bottom) up towards y0, filled `value` of the way."""
        lit = max(0.0, min(1.0, value)) * (y1 - y0 + 1)
        for i in range(y1 - y0 + 1):
            if i < int(lit):
                self.set(x, y1 - i, level)
            elif i < lit:
                self.set(x, y1 - i, max(1, round(level * (lit - i))))

    def strike(self, x: int, y: int, w: int, h: int, level: int = MAX) -> None:
        """A diagonal line from bottom-left to top-right over a w x h box: 'off'."""
        for i in range(max(w, h)):
            self.set(x + round(i * (w - 1) / max(1, max(w, h) - 1)), y + h - 1 - round(i * (h - 1) / max(1, max(w, h) - 1)), level)

    def frame(self) -> bytes:
        out = bytes(v for row in self.px for v in row)
        return out[::-1] if ROTATE_180 else out


# -- home ------------------------------------------------------------------------


@dataclass
class Channel:
    """What one strip of the home screen shows."""

    volume: float = 0.0  # 0..1, where the level sits on the knob's dB range
    fx: float | None = None  # effect amount 0..1 while an effect is on, else None
    fx_bypassed: bool = False  # an amount is set but the effect is switched off
    muted: bool = False
    knob: str | None = None  # "volume" / "fx": what its knob turns now, None if no knob
    status: str = "on"  # Bluetooth: "off" (no Bluetooth), "idle" (no phone), "connected", "playing"
    level: float = 0.0  # signal peak 0..1: the icon pulses with it while live
    slot: int = 0  # Bluetooth strip only: the pads' slot (0-based) ...
    slots: int = 1  # ... out of how many; 1 = no slot dot


def _strip(c: Canvas, ch: Channel, side: str, now: float) -> None:
    left = side == "bt"
    ix, fxcol, x0, x1 = (0, 5, 0, 5) if left else (8, 7, 7, 12)
    # what the knob changes is bright, the other one dimmer
    vol_lv = MAX if ch.knob in ("volume", None) else 3
    fx_lv = MAX if ch.knob == "fx" else 3
    # live channels pulse with their signal: the music, your voice
    pulse = 4 + round(3 * min(1.0, ch.level) ** 0.5)
    if left:
        level = {"off": DIM if int(now * 2) % 2 else 0, "idle": DIM, "connected": 4, "playing": pulse}[ch.status]
    else:
        level = pulse
    if ch.muted:
        level = DIM
    c.icon("bt" if left else "mic", ix, 0, level)
    if ch.muted or (left and ch.status == "off"):
        c.strike(ix, 0, 5, 7, MAX if ch.muted else level)
    if ch.fx is not None:
        c.vbar(fxcol, 0, 6, ch.fx, 1 if ch.fx_bypassed else fx_lv)
    c.hbar(x0, x1, 7, ch.volume, vol_lv)
    if left and ch.slots > 1:
        c.set(6, slot_row(ch.slot, ch.slots), MAX if ch.knob == "slot" else 3)


def slot_row(slot: int, slots: int) -> int:
    """Row of the home screen's slot dot in col 6: slot 1 at the top, the last at row 6."""
    return round(slot * 6 / max(1, slots - 1))


def home(bt: Channel, mic: Channel, now: float = 0.0) -> bytes:
    c = Canvas()
    _strip(c, bt, "bt", now)
    _strip(c, mic, "mic", now)
    return c.frame()


def wave(env: list[float], progress: float, bt: Channel, mic: Channel) -> bytes:
    """A pad playing: its waveform mirrored around row 3 (played part bright,
    the rest dim, so it fills left to right), the home volume bars on row 7."""
    c = Canvas()
    head = progress * COLS
    for x, v in enumerate(env[:COLS]):
        h = 0 if v < 0.01 else max(1, round(v**0.5 * 3.5))  # sqrt: closer to how loud it sounds
        lv = MAX if x < head else DIM
        for y in range(max(0, 3 - h + 1), min(7, 3 + h)):
            c.set(x, y, lv)
    for ch, (x0, x1) in ((bt, (0, 5)), (mic, (7, 12))):
        c.hbar(x0, x1, 7, ch.volume, 3)
    return c.frame()


def rec(progress: float, level: float, blink: bool) -> bytes:
    c = Canvas()
    if blink:
        c.text("REC", y=0)
    c.hbar(0, COLS - 1, 6, min(1.0, level), DIM)
    c.hbar(0, COLS - 1, 7, progress, MAX)
    return c.frame()


# -- banners ------------------------------------------------------------------------


def _side_mark(c: Canvas, side: str) -> None:
    """A line down the left edge (phone / pads) or the right edge (mic): which
    strip of the home screen the banner is about. The 3-letter text leaves both
    edge columns free."""
    x = 0 if side == "bt" else COLS - 1
    for y in range(0, 6):
        c.set(x, y, 3)


def volume(side: str, pos: float) -> bytes:
    """Turning a volume knob: the channel's icon, 0-99 and a bar."""
    c = Canvas()
    c.icon("bt" if side == "bt" else "mic", 0, 0, MAX)
    c.text("--" if pos <= 0 else f"{min(99, round(pos * 99)):02d}", x=6, y=1)
    c.hbar(0, COLS - 1, 7, pos, MAX)
    return c.frame()


def effect(side: str, short: str, amount: float, bypassed: bool = False) -> bytes:
    """Turning an effect knob or switching effect: its name and amount."""
    c = Canvas()
    label = "OFF" if bypassed else short[:3]
    # keep a dark column between the name and the side line
    x = None if len(label) < 3 else (2 if side == "bt" else 0)
    c.text(label, x=x, y=0)
    _side_mark(c, side)
    c.hbar(0, COLS - 1, 7, amount, DIM if bypassed else MAX)
    return c.frame()


def mute(side: str, muted: bool) -> bytes:
    c = Canvas()
    icon = "bt" if side == "bt" else "mic"
    if muted:
        c.icon(icon, 1, 0, DIM)
        c.icon("cross", 7, 0, MAX)
    else:
        c.icon(icon, 0, 0, MAX)
        c.text("ON", x=6, y=1)
    return c.frame()


def knob_mode(side: str, mode: str, slot: int = 0) -> bytes:
    """A knob switched to its volume, its effect or (Bluetooth knob) the pad slot."""
    c = Canvas()
    c.icon("bt" if side == "bt" else "mic", 0, 0, MAX)
    if mode == "volume":
        c.icon("speaker", 6, 0, MAX)
    elif mode == "slot":
        c.text(f"S{slot + 1}", x=6, y=1)
    else:
        c.text("FX", x=6, y=1)
    return c.frame()


def slot(index: int, count: int) -> bytes:
    """The pads moved to another slot of sounds: "S2" and a mark per slot on row 7
    (the current one bright), with the phone + pads side line."""
    c = Canvas()
    c.text(f"S{index + 1}", y=0)
    _side_mark(c, "bt")
    w = (COLS + 1) // count  # 13 cols, 4 slots: 2-wide marks, 1 apart, centred
    x0 = (COLS - (count * w - 1)) // 2
    for i in range(count):
        for x in range(x0 + i * w, x0 + i * w + w - 1):
            c.set(x, 7, MAX if i == index else 1)
    return c.frame()


EVENTS = {
    # name: [(icon, x, y), ...]
    "play": [("play", 5, 0)],
    "pause": [("pause", 4, 0)],
    "next": [("next", 2, 0)],
    "previous": [("prev", 2, 0)],
    "connected": [("bt", 1, 0), ("check", 7, 0)],
    "disconnected": [("bt", 1, 0), ("cross", 7, 0)],
    "no_phone": [("bt", 1, 0), ("cross", 7, 0)],
    "bt_off": [("bt", 2, 0), ("bang", 8, 0)],
}


def event(name: str, blink: bool = True) -> bytes:
    c = Canvas()
    for icon, x, y in EVENTS[name]:
        lv = MAX
        if name in ("disconnected", "no_phone", "bt_off") and icon == "bt":
            lv = DIM
        c.icon(icon, x, y, lv)
    if name in ("disconnected", "no_phone", "bt_off") and not blink:
        c = Canvas()  # the cross / ! blinks so a lost phone catches the eye
        for icon, x, y in EVENTS[name]:
            if icon == "bt":
                c.icon(icon, x, y, DIM)
    return c.frame()


def text(label: str, bar: float | None = None) -> bytes:
    """Plain 3-character label (pad count, module setup...)."""
    c = Canvas()
    c.text(label[:3], y=0)
    if bar is not None:
        c.hbar(0, COLS - 1, 7, bar, MAX)
    return c.frame()


SCROLL_COLS_PER_S = 14.0


def _clean(s: str) -> str:
    s = s.upper()
    return "".join(ch if ch in FONT else " " for ch in s).strip()


def scroll_length(s: str, icon: str | None = None) -> float:
    """Seconds it takes `s` (after an optional icon) to scroll all the way through."""
    width = (len(ICONS[icon][0]) + 2 if icon else 0) + len(_clean(s)) * 4
    return (width + COLS) / SCROLL_COLS_PER_S


def scroll(s: str, t: float, icon: str | None = None) -> bytes:
    """`s` scrolling right to left, `t` seconds after it started: it enters from
    the right edge and leaves on the left. An icon can lead it (a note, the BT mark)."""
    s = _clean(s)
    offset = COLS - int(t * SCROLL_COLS_PER_S)
    c = Canvas()
    x = offset
    if icon:
        x += c.icon(icon, x, 0, MAX) + 2
    c.text(s, x=x, y=1)
    return c.frame()
