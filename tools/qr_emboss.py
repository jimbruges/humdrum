#!/usr/bin/env python3
"""Wi-Fi QR code as raised geometry for the lid recess.

Credentials come from host/portal/wifi.local.env (your own, gitignored) when it
exists, otherwise from the public demo credentials in host/portal/wifi.env.
Private output goes to enclosure/local/ (gitignored) so it never gets committed;
`--demo` forces wifi.env and writes the published files in enclosure/.

The lid has a 42 x 42 x 0.8 mm recess; the dark QR modules rise from its floor
to be flush with the lid top, so a marker swiped across the tops colours the
code while the recessed quiet zone stays clean.

Outputs (in enclosure/, or enclosure/local/ for your own credentials):
  qr_emboss.json   merged rectangles in lid/world coordinates (what the CAD uses)
  qr_emboss.stl    the raised modules as a mesh, already in place: if you change
                   the password, add this to the lid in your slicer as a part
                   (and fill the old code's recess) instead of re-running the CAD

    pip install segno
    python3 tools/qr_emboss.py           # your own credentials -> enclosure/local/
    python3 tools/qr_emboss.py --demo    # public demo credentials -> enclosure/
"""

import json
import struct
import sys
from pathlib import Path

import segno

ROOT = Path(__file__).resolve().parent.parent
DEMO_ENV = ROOT / "host" / "portal" / "wifi.env"
LOCAL_ENV = ROOT / "host" / "portal" / "wifi.local.env"

# must match the lid's QrRecess feature
CENTER = (104.0, -36.0)
FLOOR_Z = 35.2
TOP_Z = 36.0
CODE_MM = 36.25  # leaves a ~2.9 mm recessed quiet zone inside the 42 mm recess
# Rectangles are shrunk this much per side so neighbours never share a face (the
# CAD kernel can't union hundreds of coincident faces). 0.12 mm gaps don't print.
INSET = 0.06


def read_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def wifi_escape(s: str) -> str:
    for ch in '\\;,:"':
        s = s.replace(ch, "\\" + ch)
    return s


def merge_rects(matrix: list[list[bool]]) -> list[tuple[int, int, int, int]]:
    """Dark modules -> (x, y, w, h) rectangles: row runs, stacked when identical."""
    open_runs: dict[tuple[int, int], list[int]] = {}  # (x, w) -> [y0, h]
    rects = []
    for y, row in enumerate(matrix + [[False] * len(matrix[0])]):
        runs, x = set(), 0
        while x < len(row):
            if row[x]:
                x0 = x
                while x < len(row) and row[x]:
                    x += 1
                runs.add((x0, x - x0))
            x += 1
        for key in list(open_runs):
            if key in runs:
                open_runs[key][1] += 1
                runs.discard(key)
            else:
                y0, h = open_runs.pop(key)
                rects.append((key[0], y0, key[1], h))
        for key in runs:
            open_runs[key] = [y, 1]
    return rects


def to_world(rects, n: int) -> list[list[float]]:
    m = CODE_MM / n
    x0 = CENTER[0] - CODE_MM / 2
    ytop = CENTER[1] + CODE_MM / 2
    out = []
    for x, y, w, h in rects:
        out.append([round(x0 + x * m + INSET, 4), round(ytop - (y + h) * m + INSET, 4),
                    round(x0 + (x + w) * m - INSET, 4), round(ytop - y * m - INSET, 4)])
    return out


def write_stl(boxes: list[list[float]], path: Path) -> None:
    tris = []
    for x0, y0, x1, y1 in boxes:
        z0, z1 = FLOOR_Z - 0.2, TOP_Z  # 0.2 mm overlap into the floor so slicers fuse it
        v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        for a, b, c in ((0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)):
            tris.append((v[a], v[b], v[c]))
    with open(path, "wb") as f:
        f.write(b"humdrum qr emboss".ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", 0, 0, 0))
            for p in t:
                f.write(struct.pack("<3f", *p))
            f.write(b"\0\0")


def main() -> None:
    use_local = LOCAL_ENV.exists() and "--demo" not in sys.argv
    env = read_env(LOCAL_ENV if use_local else DEMO_ENV)
    out = ROOT / "enclosure" / ("local" if use_local else "")
    payload = f"WIFI:T:WPA;S:{wifi_escape(env['SSID'])};P:{wifi_escape(env['PASSWORD'])};;"
    qr = segno.make(payload, error="m")
    matrix = [[bool(v) for v in row] for row in qr.matrix]
    n = len(matrix)
    rects = merge_rects(matrix)
    boxes = to_world(rects, n)
    out.mkdir(exist_ok=True)
    (out / "qr_emboss.json").write_text(json.dumps({
        "ssid": env["SSID"], "version": qr.version, "error": qr.error, "modules": n,
        "module_mm": round(CODE_MM / n, 4), "inset_mm": INSET, "floor_z": FLOOR_Z, "top_z": TOP_Z,
        "rects_xyxy": boxes,
    }, indent=1))
    write_stl(boxes, out / "qr_emboss.stl")
    which = "your wifi.local.env" if use_local else "demo wifi.env"
    print(f"QR v{qr.version}-{qr.error} from {which}: {n}x{n} modules of {CODE_MM / n:.2f} mm, "
          f"{len(rects)} rectangles -> {out.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
