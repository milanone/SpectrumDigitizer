"""Draw the program icon (SpectrumDigitizer.ico, next to SpectrumDigitizer.pyw): a spectrum curve with its digitized points on a
plot frame. Drawn large and shrunk for each size, so the small sizes stay clean.

    python examples/make_icon.py          # white tile, black axes, orange curve (default)
    python examples/make_icon.py blue     # bright blue tile
    python examples/make_icon.py light    # light tile with a blue frame
"""
import os
import struct
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "SpectrumDigitizer.ico")
SIZES = [16, 24, 32, 48, 64]      # classic sizes: no 128/256 frames, which only exist as PNG
BIG = 1024

STYLES = {   # background, frame of the tile (None: no frame), axes, curve, dots, outline of the dots
    "white": dict(bg="#ffffff", frame="#1a1a1a", axes="#000000", curve="#f28c00", dot="#000000", ring="#ffffff"),
    "light": dict(bg="#f3f7fb", frame="#2d6cb5", axes="#1f3b57", curve="#f28c00", dot="#2d6cb5", ring="#f3f7fb"),
    "blue": dict(bg="#2d6cb5", frame=None, axes="#ffffff", curve="#ffb703", dot="#ffffff", ring="#2d6cb5"),
}


def draw(style="white"):
    c = STYLES[style]
    img = Image.new("RGBA", (BIG, BIG), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([40, 40, BIG - 40, BIG - 40], radius=190, fill=c["bg"], outline=c["frame"], width=40)
    # axes: left and bottom
    d.line([(190, 190), (190, 834), (834, 834)], fill=c["axes"], width=52, joint="curve")
    d.ellipse([190 - 26, 190 - 26, 190 + 26, 190 + 26], fill=c["axes"])
    d.ellipse([834 - 26, 834 - 26, 834 + 26, 834 + 26], fill=c["axes"])

    def y_of(x):                       # two bands, like an absorption spectrum
        return 760 - 470 * np.exp(-((x - 400) / 105) ** 2) - 250 * np.exp(-((x - 650) / 85) ** 2)

    # the curve is stamped as round discs along a dense path: PIL's wide lines come out serrated
    xs = np.linspace(245, 800, 1400)
    r = 34
    for x in xs:
        y = float(y_of(x))
        d.ellipse([x - r, y - r, x + r, y + r], fill=c["curve"])
    for x in (300, 400, 530, 650, 775):             # the digitized points, spread along the curve
        y = float(y_of(x))
        d.ellipse([x - 44, y - 44, x + 44, y + 44], fill=c["dot"], outline=c["ring"], width=16)
    return img


def write_ico(path, images):
    """A classic .ico: every frame a 32-bit BMP (DIB) with alpha and an empty AND mask, smallest first. Pillow writes
    PNG-compressed frames, which Windows Vista and later read but older or simpler programs (a file manager's
    toolbar button, for one) do not."""
    entries, blobs, offset = [], [], 6 + 16 * len(images)
    for im in images:
        w, h = im.size
        px = np.asarray(im.convert("RGBA"))[::-1][..., [2, 1, 0, 3]]          # BGRA, bottom row first
        pixels = np.ascontiguousarray(px).tobytes()
        mask = bytes(((w + 31) // 32) * 4 * h)                                  # all zero: the alpha channel does the work
        header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
        blob = header + pixels + mask
        entries.append(struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(blobs))


if __name__ == "__main__":
    style = sys.argv[1] if len(sys.argv) > 1 else "white"
    big = draw(style)
    write_ico(OUT, [big.resize((s, s), Image.LANCZOS) for s in SIZES])
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(os.environ.get("TEMP", HERE), "icon_preview.png"))
    print("written", os.path.abspath(OUT), os.path.getsize(OUT), "bytes")
