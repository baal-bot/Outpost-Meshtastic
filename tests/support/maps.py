"""Tiny synthetic PMTiles v3 source; no downloaded geography or station data."""

import gzip
import json
import struct

import httpx

from outpost.maps.pmtiles import RangeSource

URL = "https://build.protomaps.com/20260908.pmtiles"


def uint(value: int) -> bytes:
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    return bytes(output) + bytes([value])


def field(number: int, value: bytes) -> bytes:
    return uint(number * 8 + 2) + uint(len(value)) + value


def vector_tile() -> bytes:
    # MVT polygon spanning a tile: MoveTo(0,0), LineTo(4096,0/4096/0), ClosePath.
    geometry = b"".join(uint(n) for n in [9, 0, 0, 26, 8192, 0, 0, 8192, 8191, 0, 15])
    polygon = b"\x18\x03" + field(4, geometry)
    layer = field(1, b"earth") + field(2, polygon) + b"\x28\x80\x20\x78\x02"
    # A place label exercises local glyph fetching in the offline browser test.
    point = field(2, b"\x00\x00") + b"\x18\x01" + field(4, b"\x09\x80\x20\x80\x20")
    places = (
        field(1, b"places")
        + field(2, point)
        + field(3, b"name")
        + field(4, field(1, b"Test city"))
        + b"\x28\x80\x20\x78\x02"
    )
    return field(3, layer) + field(3, places)


def pmtiles() -> bytes:
    tile = gzip.compress(vector_tile(), mtime=0)
    # One run maps every supported Hilbert tile id to the same synthetic tile.
    root = gzip.compress(b"".join(uint(n) for n in [1, 0, (4**16 - 1) // 3, len(tile), 1]), mtime=0)
    meta = gzip.compress(
        json.dumps(
            {
                "version": "4.15.2",
                "vector_layers": [{"id": value} for value in ["earth", "water", "roads", "places"]],
            }
        ).encode(),
        mtime=0,
    )
    header = bytearray(127)
    header[:8] = b"PMTiles\x03"
    data_offset = 127 + len(root) + len(meta)
    struct.pack_into(
        "<11Q",
        header,
        8,
        127,
        len(root),
        127 + len(root),
        len(meta),
        data_offset,
        0,
        data_offset,
        len(tile),
        (4**16 - 1) // 3,
        1,
        1,
    )
    header[96:102] = bytes([1, 2, 2, 1, 0, 15])
    return bytes(header) + root + meta + tile


def source_factory(calls=None):
    data = pmtiles()

    def respond(request):
        if calls is not None:
            calls.append(request)
        start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
        return httpx.Response(
            206,
            stream=httpx.ByteStream(data[start : end + 1]),
            headers={"Content-Range": f"bytes {start}-{end}/{len(data)}", "ETag": '"test-v1"'},
        )

    def factory(url=URL, **kwargs):
        kwargs.setdefault("budget", 32 * 1024**2)
        return RangeSource(
            url, client=httpx.Client(transport=httpx.MockTransport(respond)), **kwargs
        )

    return factory
