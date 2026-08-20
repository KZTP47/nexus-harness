"""Draw the icon the desktop launcher wears.

Drawn here rather than kept only as a picture nobody can read, so it can be
looked at, argued with and made again. Nothing outside the standard library is
used: an icon that needs a drawing program installed before anybody can change
it is an icon nobody changes.

The mark is the agent board in miniature - three agents along the top, one
project below, lines from each agent down to it. That is what the harness is
for, it is the tab people are opening it to reach, and it still reads as a small
network at sixteen pixels across, which is the size that decides whether an icon
works.

    python scripts/draw_the_icon.py            writes the icon
    python scripts/draw_the_icon.py --check    says whether the one on disk
                                               is the one this draws
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHERE_IT_GOES = ROOT / "desktop" / "nexus-harness.ico"

# The sizes Windows asks for. Sixteen is the one in a corner of the taskbar and
# the one that decides whether a mark works; two hundred and fifty six is what a
# large tile and the installer want.
SIZES = (256, 128, 64, 48, 32, 16)

# Drawn this many times larger and then averaged down, which is how a shape gets
# a smooth edge without a drawing library.
FINER = 4

# The panel's own colours, so the icon and the window it opens look like one
# thing.
BEHIND = (7, 25, 34, 255)          # the deep teal the panel sits on
EDGE = (99, 230, 255, 255)         # the cyan of a picked box
AGENT = (99, 230, 255, 255)
PROJECT = (137, 247, 161, 255)     # the green of a project
LINE = (255, 142, 156, 255)        # the red of a works-on line
NOTHING = (0, 0, 0, 0)


class Paper:
    """Somewhere to put pixels, and the few shapes this needs."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.pixels = [NOTHING] * (size * size)

    def put(self, x: int, y: int, colour: tuple[int, int, int, int]) -> None:
        if 0 <= x < self.size and 0 <= y < self.size:
            self.pixels[y * self.size + x] = colour

    def rounded_square(self, radius: int, colour: tuple[int, int, int, int]) -> None:
        """The whole paper, with its corners taken off."""

        size = self.size
        for y in range(size):
            for x in range(size):
                across = min(x, size - 1 - x)
                down = min(y, size - 1 - y)
                if across < radius and down < radius:
                    away = ((radius - across) ** 2 + (radius - down) ** 2) ** 0.5
                    if away > radius:
                        continue
                self.put(x, y, colour)

    def disc(self, at: tuple[float, float], radius: float,
             colour: tuple[int, int, int, int]) -> None:
        middle_x, middle_y = at
        low_x, high_x = int(middle_x - radius) - 1, int(middle_x + radius) + 1
        low_y, high_y = int(middle_y - radius) - 1, int(middle_y + radius) + 1
        for y in range(low_y, high_y + 1):
            for x in range(low_x, high_x + 1):
                if (x + 0.5 - middle_x) ** 2 + (y + 0.5 - middle_y) ** 2 <= radius ** 2:
                    self.put(x, y, colour)

    def line(self, start: tuple[float, float], end: tuple[float, float],
             thickness: float, colour: tuple[int, int, int, int]) -> None:
        """A line with round ends, drawn as a row of discs along it."""

        away_x, away_y = end[0] - start[0], end[1] - start[1]
        how_far = max(1, int((away_x ** 2 + away_y ** 2) ** 0.5))
        for step in range(how_far + 1):
            part = step / how_far
            self.disc(
                (start[0] + away_x * part, start[1] + away_y * part),
                thickness / 2, colour,
            )


def _over(top: tuple[int, int, int, int],
          bottom: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """One colour laid over another, both of which may be see-through."""

    top_alpha = top[3] / 255
    bottom_alpha = bottom[3] / 255
    alpha = top_alpha + bottom_alpha * (1 - top_alpha)
    if alpha <= 0:
        return NOTHING
    return tuple(  # type: ignore[return-value]
        round((top[n] * top_alpha + bottom[n] * bottom_alpha * (1 - top_alpha)) / alpha)
        for n in range(3)
    ) + (round(alpha * 255),)


def _smaller(paper: Paper, size: int) -> list[tuple[int, int, int, int]]:
    """The drawing again at the size wanted, by averaging squares of it."""

    step = paper.size // size
    made = []
    for y in range(size):
        for x in range(size):
            red = green = blue = alpha = 0
            for down in range(step):
                for across in range(step):
                    one = paper.pixels[(y * step + down) * paper.size + x * step + across]
                    weight = one[3]
                    red += one[0] * weight
                    green += one[1] * weight
                    blue += one[2] * weight
                    alpha += weight
            if alpha == 0:
                made.append(NOTHING)
                continue
            made.append((
                round(red / alpha), round(green / alpha), round(blue / alpha),
                round(alpha / (step * step)),
            ))
    return made


def draw_one(size: int) -> list[tuple[int, int, int, int]]:
    """The mark, at one size."""

    big = size * FINER
    paper = Paper(big)
    paper.rounded_square(round(big * 0.22), BEHIND)

    # Three agents along the top, one project below the middle of them.
    agents = [(big * 0.24, big * 0.32), (big * 0.5, big * 0.26), (big * 0.76, big * 0.32)]
    project = (big * 0.5, big * 0.74)
    thick = max(2.0, big * 0.045)
    for one in agents:
        paper.line(one, project, thick, LINE)
    for one in agents:
        paper.disc(one, big * 0.105, AGENT)
    paper.disc(project, big * 0.125, PROJECT)

    # A thin edge, so the mark keeps its shape on a light background as well as
    # a dark one.
    edge = Paper(big)
    edge.rounded_square(round(big * 0.22), EDGE)
    inside = Paper(big)
    inside.rounded_square(round(big * 0.22) - max(1, round(big * 0.02)), (0, 0, 0, 255))
    for at, held in enumerate(inside.pixels):
        if held[3]:
            edge.pixels[at] = NOTHING
    for at, held in enumerate(edge.pixels):
        if held[3]:
            paper.pixels[at] = _over(held, paper.pixels[at])

    return _smaller(paper, size)


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def as_png(pixels: list[tuple[int, int, int, int]], size: int) -> bytes:
    """One drawing, written as a PNG.

    Icons hold whole PNG files, one for each size, which is why this is here.
    """

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # this row is written plainly, not worked out from the last
        for x in range(size):
            rows.extend(bytes(pixels[y * size + x]))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )


def as_icon() -> bytes:
    """Every size, in the one file Windows wants."""

    drawings = [(size, as_png(draw_one(size), size)) for size in SIZES]
    head = struct.pack("<HHH", 0, 1, len(drawings))
    at = len(head) + 16 * len(drawings)
    entries = bytearray()
    bodies = bytearray()
    for size, body in drawings:
        # Two hundred and fifty six is written as nothing, which is how the
        # format says "the big one".
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(body), at)
        bodies += body
        at += len(body)
    return head + bytes(entries) + bytes(bodies)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Say whether the icon on disk is the one this draws, and change nothing",
    )
    parser.add_argument("--output", default=str(WHERE_IT_GOES))
    said = parser.parse_args(argv)
    drawn = as_icon()
    where = Path(said.output)
    if said.check:
        if where.is_file() and where.read_bytes() == drawn:
            print(f"{where} is the icon this draws.")
            return 0
        print(f"{where} is not the icon this draws. Run: python {Path(__file__).name}")
        return 1
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_bytes(drawn)
    print(f"Wrote {where} ({len(drawn)} bytes, sizes {', '.join(str(one) for one in SIZES)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
