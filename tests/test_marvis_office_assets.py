from __future__ import annotations

import struct
import zlib
from pathlib import Path


ASSET_DIR = Path(__file__).resolve().parents[1] / "wlcodex" / "live_stream" / "static" / "marvis"


def _read_png_rgba(path: Path) -> tuple[int, int, bytes]:
    raw = path.read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    width = height = color_type = None
    payload = bytearray()
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        chunk_type = raw[offset + 4 : offset + 8]
        chunk_data = raw[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, _ = struct.unpack(">IIBBBBB", chunk_data)
            assert bit_depth == 8
            assert color_type in (2, 6)
        elif chunk_type == b"IDAT":
            payload.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    assert width is not None and height is not None and color_type is not None
    channels = 4 if color_type == 6 else 3
    stride = width * channels
    decompressed = zlib.decompress(bytes(payload))
    rows: list[bytearray] = []
    cursor = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = decompressed[cursor]
        cursor += 1
        row = bytearray(decompressed[cursor : cursor + stride])
        cursor += stride
        for index, value in enumerate(row):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            up_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (value + left) & 0xFF
            elif filter_type == 2:
                row[index] = (value + up) & 0xFF
            elif filter_type == 3:
                row[index] = (value + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                predictor = left + up - up_left
                choices = (abs(predictor - left), abs(predictor - up), abs(predictor - up_left))
                row[index] = (value + (left, up, up_left)[choices.index(min(choices))]) & 0xFF
            else:
                assert filter_type == 0
        rows.append(row)
        previous = row

    if channels == 4:
        return width, height, b"".join(rows)

    rgba = bytearray()
    for row in rows:
        for index in range(0, len(row), 3):
            rgba.extend(row[index : index + 3])
            rgba.append(255)
    return width, height, bytes(rgba)


def _count_pixels(
    pixels: bytes,
    width: int,
    *,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    red: bool,
) -> int:
    total = 0
    for y in range(y1, y2):
        for x in range(x1, x2):
            index = (y * width + x) * 4
            r, g, b, a = pixels[index : index + 4]
            if a < 200:
                continue
            if red and r > 200 and g < 90 and b < 80:
                total += 1
            if not red and g > 130 and 60 < r < 150 and 50 < b < 150 and g - r > 30:
                total += 1
    return total


def test_marvis_office_scene_puts_red_director_first_and_green_architect_second() -> None:
    width, _, pixels = _read_png_rgba(ASSET_DIR / "office-scene-roles-5.png")

    first_slot_red = _count_pixels(
        pixels,
        width,
        x1=200,
        y1=145,
        x2=355,
        y2=190,
        red=True,
    )
    second_slot_green = _count_pixels(
        pixels,
        width,
        x1=700,
        y1=145,
        x2=850,
        y2=190,
        red=False,
    )

    assert first_slot_red > 1000
    assert second_slot_green > 1000
