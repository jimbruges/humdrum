"""Frames for the UNO Q's 8x13 LED matrix, visible through the lid window.

    rows 0-4  three-letter label (current FX, REC, pad count)
    row  6    output level
    row  7    knob amount / recording progress

While a pad plays, rows 0-5 show its waveform instead (see render_wave).
"""

from __future__ import annotations

ROWS, COLS = 8, 13
MAX = 7  # 3-bit grayscale
ROTATE_180 = True

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
    "H": ["#.#", "#.#", "###", "#.#", "#.#"],
    "I": ["###", ".#.", ".#.", ".#.", "###"],
    "N": ["#.#", "###", "###", "#.#", "#.#"],
    "O": [".#.", "#.#", "#.#", "#.#", ".#."],
    "P": ["##.", "#.#", "##.", "#..", "#.."],
    "R": ["##.", "#.#", "##.", "#.#", "#.#"],
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "U": ["#.#", "#.#", "#.#", "#.#", "###"],
    "V": ["#.#", "#.#", "#.#", "#.#", ".#."],
    "W": ["#.#", "#.#", "###", "###", "#.#"],
    "-": ["...", "...", "###", "...", "..."],
    " ": ["...", "...", "...", "...", "..."],
}


def _text(frame: list[list[int]], text: str, level: int) -> None:
    text = text[:3]
    width = len(text) * 4 - 1
    x0 = (COLS - width) // 2
    for i, ch in enumerate(text.upper()):
        glyph = FONT.get(ch, FONT["-"])
        for r, row in enumerate(glyph):
            for c, px in enumerate(row):
                if px == "#":
                    frame[r][x0 + i * 4 + c] = level


def _bar(frame: list[list[int]], row: int, value: float, level: int) -> None:
    lit = value * COLS
    for c in range(COLS):
        if c < int(lit):
            frame[row][c] = level
        elif c < lit:
            frame[row][c] = max(1, int(level * (lit - c)))  # partial last pixel


def _finish(f: list[list[int]]) -> bytes:
    out = bytes(v for row in f for v in row)
    # the enclosure mounts the UNO Q rotated 180° (USB-C to the right wall)
    return out[::-1] if ROTATE_180 else out


def render(label: str, amount: float, level: float, dim: bool = False) -> bytes:
    f = [[0] * COLS for _ in range(ROWS)]
    _text(f, label, 2 if dim else MAX)
    _bar(f, 6, min(1.0, level), 2)
    _bar(f, 7, min(1.0, amount), MAX)
    return _finish(f)


WAVE_PLAYED = MAX
WAVE_AHEAD = 2


def render_wave(env: list[float], progress: float, amount: float, level: float) -> bytes:
    """Sample waveform mirrored around the middle of rows 0-5; the part already
    played is bright and the rest dim, so it fills left to right as it plays."""
    f = [[0] * COLS for _ in range(ROWS)]
    head = progress * COLS
    for c, v in enumerate(env[:COLS]):
        # sqrt is closer to how loud it sounds, so decaying tails stay visible
        h = 0 if v < 0.01 else max(1, round(v**0.5 * 3))  # 1..3 rows each side of the centre
        lv = WAVE_PLAYED if c < head else WAVE_AHEAD
        for r in range(3 - h, 3 + h):
            f[r][c] = lv
    _bar(f, 6, min(1.0, level), 2)
    _bar(f, 7, min(1.0, amount), MAX)
    return _finish(f)
