"""Reading, writing, and comparing PNG pictures, using nothing but Python.

Screenshot checks need to know whether a page still looks the way it did. That
means opening two PNG files and counting the pixels that changed. Python can
already unpack the compressed data, so the only real work is undoing the row
filters PNG uses and turning whatever color form the file uses into plain red,
green, blue and see-through values.

The older tool this replaces got three things wrong, so this one is careful
about them:

- It ignored the see-through value, so a box that faded out looked unchanged.
  Here all four values count.
- It gave up or quietly passed when the two pictures were different sizes.
  Here a size change is a real difference, and the extra area counts as changed.
- It turned the allowed amount into a share of the picture twice, so asking for
  zero really meant five percent. Here a number is converted once, by one
  function, and zero means zero.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Sequence

from .models import HarnessError

SIGNATURE = b"\x89PNG\r\n\x1a\n"
# A screen is a few million pixels. Ten times that is already far past anything a
# page screenshot produces, and it keeps a broken file from eating the machine.
MAX_PIXELS = 40_000_000
MAX_SIDE = 20_000
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
# Turns a brightness into the pale grey used for the unchanged parts of a
# difference picture. Kept as a table so a whole row can be washed out at once.
_GHOST = bytes(255 - (255 - value) // 4 for value in range(256))


class ImageError(HarnessError):
    """A picture problem the user can understand and fix."""


@dataclass(frozen=True)
class Image:
    """One picture, held as four values per pixel: red, green, blue, see-through."""

    width: int
    height: int
    pixels: bytes

    def __post_init__(self) -> None:
        if len(self.pixels) != self.width * self.height * 4:
            raise ImageError("The picture data does not match its width and height")

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    def at(self, x: int, y: int) -> tuple[int, int, int, int]:
        start = (y * self.width + x) * 4
        return tuple(self.pixels[start : start + 4])  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _samples(row: bytes, width: int, channels: int, depth: int, scale: bool) -> list[int]:
    """Pull one row apart into single numbers, whatever size those numbers are."""

    wanted = width * channels
    if depth == 8:
        return list(row[:wanted])
    if depth == 16:
        # Two bytes per number. The first byte holds almost all of the color,
        # so keeping it loses nothing anybody can see.
        return list(row[0 : wanted * 2 : 2])
    values: list[int] = []
    highest = (1 << depth) - 1
    per_byte = 8 // depth
    for index in range(wanted):
        byte = row[index // per_byte]
        shift = 8 - depth * (index % per_byte + 1)
        value = (byte >> shift) & highest
        values.append(value * 255 // highest if scale else value)
    return values


def _undo_filters(raw: bytes, height: int, stride: int, bpp: int, label: str) -> list[bytes]:
    """Every PNG row is stored as a difference from its neighbours. Put it back."""

    if len(raw) < height * (stride + 1):
        raise ImageError(f"{label} is missing picture data")
    rows: list[bytes] = []
    previous = bytearray(stride)
    position = 0
    for number in range(height):
        method = raw[position]
        position += 1
        line = bytearray(raw[position : position + stride])
        position += stride
        if method == 0:
            pass
        elif method == 1:
            for index in range(bpp, stride):
                line[index] = (line[index] + line[index - bpp]) & 0xFF
        elif method == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif method == 3:
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif method == 4:
            for index in range(stride):
                left = line[index - bpp] if index >= bpp else 0
                up = previous[index]
                corner = previous[index - bpp] if index >= bpp else 0
                guess = left + up - corner
                to_left = abs(guess - left)
                to_up = abs(guess - up)
                to_corner = abs(guess - corner)
                if to_left <= to_up and to_left <= to_corner:
                    nearest = left
                elif to_up <= to_corner:
                    nearest = up
                else:
                    nearest = corner
                line[index] = (line[index] + nearest) & 0xFF
        else:
            raise ImageError(f"{label} row {number + 1} uses an unknown filter: {method}")
        rows.append(bytes(line))
        previous = line
    return rows


def read_png(data: bytes, label: str = "the picture") -> Image:
    """Open a PNG file. Anything odd is refused with a sentence, not a crash."""

    if not isinstance(data, (bytes, bytearray)):
        raise ImageError(f"{label} must be file data")
    data = bytes(data)
    if not data.startswith(SIGNATURE):
        raise ImageError(f"{label} is not a PNG file")
    position = len(SIGNATURE)
    header: tuple[int, int, int, int, int] | None = None
    palette = b""
    see_through = b""
    parts: list[bytes] = []
    finished = False
    while position + 8 <= len(data):
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        start = position + 8
        end = start + length
        if length > len(data) or end + 4 > len(data):
            raise ImageError(f"{label} stops in the middle of a {kind.decode('ascii', 'replace')} part")
        body = data[start:end]
        # Every part of a PNG carries a check number worked out from its
        # contents. If it does not match, the file was damaged on the way here,
        # and a damaged saved picture must never be compared with a fresh one.
        written = int.from_bytes(data[end : end + 4], "big")
        if written != (zlib.crc32(kind + body) & 0xFFFFFFFF):
            raise ImageError(
                f"{label} is damaged: the {kind.decode('ascii', 'replace')} part does not match "
                "its own check number. Save the picture again."
            )
        position = end + 4
        if kind == b"IHDR":
            if len(body) != 13:
                raise ImageError(f"{label} has a broken header")
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            depth, color, packing, filtering, interlace = body[8:13]
            if width < 1 or height < 1 or width > MAX_SIDE or height > MAX_SIDE:
                raise ImageError(f"{label} says it is {width} by {height}, which cannot be right")
            if width * height > MAX_PIXELS:
                raise ImageError(f"{label} is larger than {MAX_PIXELS} pixels, which is too big to compare")
            if color not in _CHANNELS:
                raise ImageError(f"{label} uses a color form this tool cannot read: {color}")
            if depth not in (1, 2, 4, 8, 16) or (color != 3 and depth < 8):
                raise ImageError(f"{label} uses {depth} bits per color, which this tool cannot read")
            if packing != 0 or filtering != 0:
                raise ImageError(f"{label} is packed in a way this tool cannot read")
            if interlace != 0:
                raise ImageError(
                    f"{label} is saved in the interlaced form. Save it again as a plain PNG."
                )
            header = (width, height, depth, color, interlace)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            see_through = body
        elif kind == b"IDAT":
            parts.append(body)
        elif kind == b"IEND":
            finished = True
            break
    if header is None:
        raise ImageError(f"{label} has no header, so it is not a usable PNG file")
    if not finished:
        # Every whole PNG file ends with an end marker. Without one the file was
        # cut short, and half a picture must not be compared with a whole one.
        raise ImageError(f"{label} stops in the middle, so the file is not complete")
    if not parts:
        raise ImageError(f"{label} holds no picture data")
    width, height, depth, color, _ = header
    channels = _CHANNELS[color]
    stride = (width * channels * depth + 7) // 8
    # A picture of this size cannot unpack to more than this. A small file can
    # ask for any amount of memory otherwise: 400 kilobytes on disk claiming to
    # be one pixel across unpacked to eight hundred megabytes and then handed
    # back a one pixel picture as though nothing had happened.
    most = height * (stride + 1)
    try:
        unpacker = zlib.decompressobj()
        raw = unpacker.decompress(b"".join(parts), most + 1)
    except zlib.error as exc:
        raise ImageError(f"{label} could not be unpacked: {exc}") from exc
    if len(raw) > most:
        raise ImageError(
            f"{label} unpacks to more than a {width} by {height} picture can hold, "
            "so the file does not say what it really is"
        )
    bpp = max(1, channels * depth // 8)
    rows = _undo_filters(raw, height, stride, bpp, label)
    if color == 3 and not palette:
        raise ImageError(f"{label} uses a color list but does not include one")
    return Image(width, height, _to_rgba(rows, width, height, channels, depth, color, palette, see_through))


def _to_rgba(
    rows: Sequence[bytes],
    width: int,
    height: int,
    channels: int,
    depth: int,
    color: int,
    palette: bytes,
    see_through: bytes,
) -> bytes:
    out = bytearray(width * height * 4)
    at = 0
    for row in rows:
        values = _samples(row, width, channels, depth, scale=color != 3)
        for index in range(width):
            base = index * channels
            if color == 0:
                grey = values[base]
                out[at : at + 4] = bytes((grey, grey, grey, 255))
            elif color == 4:
                grey = values[base]
                out[at : at + 4] = bytes((grey, grey, grey, values[base + 1]))
            elif color == 2:
                out[at : at + 4] = bytes((values[base], values[base + 1], values[base + 2], 255))
            elif color == 6:
                out[at : at + 4] = bytes(
                    (values[base], values[base + 1], values[base + 2], values[base + 3])
                )
            else:
                slot = values[base]
                start = slot * 3
                if start + 3 > len(palette):
                    raise ImageError("The picture points at a color that is not in its color list")
                alpha = see_through[slot] if slot < len(see_through) else 255
                out[at : at + 4] = bytes(
                    (palette[start], palette[start + 1], palette[start + 2], alpha)
                )
            at += 4
    return bytes(out)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (
        len(body).to_bytes(4, "big")
        + kind
        + body
        + (zlib.crc32(kind + body) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def write_png(image: Image) -> bytes:
    """Save a picture as a plain PNG file, one filter byte per row."""

    header = (
        image.width.to_bytes(4, "big")
        + image.height.to_bytes(4, "big")
        + bytes((8, 6, 0, 0, 0))
    )
    stride = image.width * 4
    body = bytearray()
    for y in range(image.height):
        body.append(0)
        body += image.pixels[y * stride : (y + 1) * stride]
    return (
        SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(body), 6))
        + _chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Difference:
    """What changed between two pictures."""

    width: int
    height: int
    changed: int
    compared: int
    same_size: bool
    before_size: tuple[int, int]
    after_size: tuple[int, int]
    biggest_channel_gap: int
    picture: Image

    @property
    def percent(self) -> float:
        """The share of pixels that changed, from 0 to 100. Worked out once, here."""

        if self.compared <= 0:
            return 100.0 if self.changed else 0.0
        return 100.0 * self.changed / self.compared

    def summary(self) -> str:
        if not self.same_size:
            return (
                f"the picture is now {self.after_size[0]} by {self.after_size[1]} pixels "
                f"instead of {self.before_size[0]} by {self.before_size[1]}, "
                f"and {self.changed} of {self.compared} pixels differ ({self.percent:.2f}%)"
            )
        return f"{self.changed} of {self.compared} pixels differ ({self.percent:.2f}%)"


def compare(before: Image, after: Image, tolerance: int = 0) -> Difference:
    """Count the pixels that changed.

    `tolerance` is how far one color value may drift, from 0 to 255, before it
    counts as a change. It is a per value amount, not a share of the picture.

    A pixel counts as changed when any of its four values, the see-through one
    included, moved further than that. Where the two pictures are different
    sizes, every pixel that only one of them has counts as changed, because a page
    that grew or shrank has changed.
    """

    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 0 <= tolerance <= 255:
        raise ImageError("The allowed color drift must be a whole number from 0 to 255")
    width = max(before.width, after.width)
    height = max(before.height, after.height)
    if width * height > MAX_PIXELS:
        raise ImageError("The two pictures together are too big to compare")
    same_size = (before.width, before.height) == (after.width, after.height)
    old = before.pixels
    new = after.pixels
    out = bytearray(width * height * 4)
    changed = 0
    biggest = 0
    # A whole page is a million pixels or more, so the work is done a row at a
    # time. Rows that match are handled by one comparison and a few whole-row
    # copies; only a row that really changed is walked pixel by pixel.
    missing_row = b"\xff\x00\xff\xff" * width
    shared_width = min(before.width, after.width)
    shared_height = min(before.height, after.height)
    before_stride = before.width * 4
    after_stride = after.width * 4
    tail = width - shared_width
    tail_row = b"\xff\x00\xff\xff" * tail
    for y in range(height):
        row_at = y * width * 4
        if y >= shared_height:
            # A row only one picture has. All of it is new or gone.
            out[row_at : row_at + width * 4] = missing_row
            changed += width
            continue
        old_row = old[y * before_stride : y * before_stride + shared_width * 4]
        new_row = new[y * after_stride : y * after_stride + shared_width * 4]
        # Keep the old page underneath as a faint grey ghost, so a person can
        # see where on the page the red marks sit. Brightness comes from the
        # green value, which is the one the eye follows most.
        line = bytearray(b"\xff" * (shared_width * 4))
        ghost = old_row[1::4].translate(_GHOST)
        line[0::4] = ghost
        line[1::4] = ghost
        line[2::4] = ghost
        if old_row != new_row:
            for x in range(shared_width):
                at = x * 4
                was = old_row[at : at + 4]
                now = new_row[at : at + 4]
                if was == now:
                    continue
                gap = max(abs(was[step] - now[step]) for step in range(4))
                if gap > biggest:
                    biggest = gap
                if gap > tolerance:
                    changed += 1
                    line[at : at + 4] = b"\xff\x30\x30\xff"
        out[row_at : row_at + shared_width * 4] = line
        if tail:
            out[row_at + shared_width * 4 : row_at + width * 4] = tail_row
            changed += tail
    return Difference(
        width=width,
        height=height,
        changed=changed,
        compared=width * height,
        same_size=same_size,
        before_size=(before.width, before.height),
        after_size=(after.width, after.height),
        biggest_channel_gap=biggest,
        picture=Image(width, height, bytes(out)),
    )
