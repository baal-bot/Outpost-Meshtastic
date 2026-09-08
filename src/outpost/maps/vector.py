"""Bounded structural validation of MVT protobufs before pack selection."""

from collections.abc import Iterator

from outpost.maps.pmtiles import expand, varint
from outpost.maps.regions import MapError


def fields(raw: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    cursor = 0
    while cursor < len(raw):
        tag, cursor = varint(raw, cursor)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < 2**29:
            raise MapError("Invalid vector tile field.")
        if wire == 0:
            value, cursor = varint(raw, cursor)
            yield number, wire, value
            continue
        if wire == 2:
            length, cursor = varint(raw, cursor)
        elif wire in {1, 5}:
            length = 8 if wire == 1 else 4
        else:
            raise MapError("Unsupported vector tile field encoding.")
        if cursor + length > len(raw):
            raise MapError("Truncated vector tile field.")
        yield number, wire, raw[cursor : cursor + length]
        cursor += length


def validate_tile(data: bytes, compression: int) -> None:
    if not data:  # The PMTiles directory may intentionally omit featureless ocean tiles.
        return
    raw = expand(data, compression)
    for number, wire, value in fields(raw):
        if number != 3:
            continue
        if wire != 2 or not isinstance(value, bytes):
            raise MapError("Invalid vector tile layer.")
        name = version = False
        for field, kind, content in fields(value):
            if field == 1:
                name = isinstance(content, bytes) and 0 < len(content) <= 256
            elif field == 15:
                version = kind == 0 and content in {1, 2}
            elif field == 2:
                if not isinstance(content, bytes):
                    raise MapError("Invalid vector tile feature.")
                for item, encoding, body in fields(content):
                    if item in {2, 4}:
                        if encoding != 2 or not isinstance(body, bytes):
                            raise MapError("Invalid vector tile feature geometry or tags.")
                        cursor = 0
                        while cursor < len(body):
                            _, cursor = varint(body, cursor)
        if not name or not version:
            raise MapError("Vector tile layer has no supported name or version.")
