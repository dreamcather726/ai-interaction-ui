"""Pure Python scene generation; no display, camera, NumPy or OpenCV required."""

import math
from functools import lru_cache

STATIONS = {"station_a": (-2.0, 0.0), "station_b": (2.0, 0.0)}
FONT = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ":": ("000", "010", "000", "010", "000"),
    "-": ("000", "000", "111", "000", "000"),
    " ": ("000",) * 5,
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
}


def glyph_pixels(text, x, y, scale=1):
    for index, character in enumerate(text):
        for row, bits in enumerate(FONT.get(character, FONT[" "])):
            for column, bit in enumerate(bits):
                if bit == "1":
                    for dy in range(scale):
                        for dx in range(scale):
                            yield x + (
                                index * 4 + column
                            ) * scale + dx, y + row * scale + dy


def occupancy_grid(*, show_labels=True):
    """200 x 200 cells, origin (-5,-5), 5 cm/cell; all unoccupied cells known free."""
    size = 200
    cells = [0] * (size * size)

    def mark(x, y):
        if 0 <= x < size and 0 <= y < size:
            cells[y * size + x] = 100

    for index in range(size):
        for offset in (0, 1):
            for x, y in (
                (index, offset),
                (index, 199 - offset),
                (offset, index),
                (199 - offset, index),
            ):
                mark(x, y)
    # Rectangular workcell boundary, leaving doorways through the two side walls.
    for x in range(40, 161):
        mark(x, 70)
        mark(x, 130)
    for y in range(70, 131):
        if not 92 <= y <= 108:
            mark(40, y)
            mark(160, y)
    for text, x in ((("A", 58), ("B", 138)) if show_labels else ()):
        for px, py in glyph_pixels(text, x, 113, 2):
            mark(px, 113 - (py - 113))  # Bitmap rows run downward; map Y runs upward.
    return cells


@lru_cache(maxsize=2)
def background(width, height):
    """Build the static gradient once, rather than repainting every pixel at 10 Hz."""
    frame = bytearray(width * height * 3)
    for y in range(height):
        row = bytearray()
        for x in range(width):
            grid = 28 if x % 40 < 2 or y % 40 < 2 else 0
            row.extend(
                (
                    min(255, 30 + x * 110 // width + grid),
                    min(255, 40 + y * 110 // height + grid),
                    105 + grid,
                )
            )
        frame[y * width * 3 : (y + 1) * width * 3] = row
    return bytes(frame)


def rgb_frame(width, height, elapsed, timestamp):
    """Colored gradient/grid plus a timestamp and a smoothly moving circle."""
    frame = bytearray(background(width, height))
    radius = max(4, min(width, height) // 20)
    cx = round(width * (0.5 + 0.3 * math.sin(elapsed * 0.7)))
    cy = round(height * (0.55 + 0.22 * math.cos(elapsed * 0.9)))
    for y in range(max(0, cy - radius), min(height, cy + radius + 1)):
        for x in range(max(0, cx - radius), min(width, cx + radius + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius**2:
                offset = (y * width + x) * 3
                frame[offset : offset + 3] = b"\xff\xbd\x35"
    scale = max(1, min(3, width // 90))
    for x, y in glyph_pixels(timestamp, 8, 8, scale):
        if x < width and y < height:
            offset = (y * width + x) * 3
            frame[offset : offset + 3] = b"\xff\xff\xff"
    return bytes(frame)


def patrol(elapsed, speed=0.35):
    phase = elapsed * speed % 8.0
    going_right = phase < 4.0
    x = -2.0 + phase if going_right else 6.0 - phase
    return x, 0.02 * math.sin(elapsed), 0.0 if going_right else math.pi, speed
