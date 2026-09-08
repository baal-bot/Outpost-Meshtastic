"""Bounded PMTiles v3 range reader; implements the public-domain PMTiles specification.

https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md
Only MVT archives with gzip/uncompressed internals and tiles are supported.
"""

from __future__ import annotations

import bisect
import json
import re
import struct
import threading
import zlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from outpost.maps.regions import MAX_DOWNLOAD_BYTES, MapError

MAX_RANGE = 4 * 1024**2
MAX_EXPANDED = 16 * 1024**2


def expand(value: bytes, compression: int, limit: int = MAX_EXPANDED) -> bytes:
    if compression == 1:
        if len(value) > limit:
            raise MapError("Map data exceeds its supported size limit.")
        return value
    if compression != 2:
        raise MapError("The map source uses unsupported compression.")
    decoder = zlib.decompressobj(31)
    try:
        result = decoder.decompress(value, limit + 1)
    except zlib.error as error:
        raise MapError("Compressed map data is corrupt.") from error
    if len(result) > limit or not decoder.eof or decoder.unused_data:
        raise MapError("Compressed map data is incomplete or exceeds its size limit.")
    return result


def tile_id(zoom: int, x: int, y: int) -> int:
    if not 0 <= zoom <= 15 or not 0 <= x < 1 << zoom or not 0 <= y < 1 << zoom:
        raise MapError("Map tile coordinates are invalid.")
    result = ((1 << (2 * zoom)) - 1) // 3
    step = (1 << zoom) // 2
    while step:
        rx, ry = bool(x & step), bool(y & step)
        result += step * step * ((3 * int(rx)) ^ int(ry))
        if not ry:
            if rx:
                x, y = step - 1 - x, step - 1 - y
            x, y = y, x
        step //= 2
    return result


def varint(data: bytes, cursor: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if cursor >= len(data):
            break
        byte = data[cursor]
        cursor += 1
        value |= (byte & 127) << shift
        if byte < 128:
            if value > 0xFFFFFFFFFFFFFFFF:
                break
            return value, cursor
    raise MapError("The map source has an invalid directory.")


@dataclass(frozen=True)
class Entry:
    identifier: int
    offset: int
    length: int
    run: int


def directory(data: bytes, compression: int) -> tuple[list[int], list[Entry]]:
    raw = expand(data, compression)
    count, cursor = varint(raw, 0)
    if not 0 < count <= 100_000:
        raise MapError("The map source directory exceeds its entry limit.")
    columns: list[list[int]] = []
    for column in range(4):
        values: list[int] = []
        for index in range(count):
            value, cursor = varint(raw, cursor)
            if column == 0:
                value += values[-1] if values else 0
                if index and value <= values[-1]:
                    raise MapError("Map source tile ordering is invalid.")
            if column == 3:
                value = values[-1] + columns[2][index - 1] if value == 0 and index else value - 1
            values.append(value)
        columns.append(values)
    if cursor != len(raw):
        raise MapError("Map source directory contains trailing data.")
    entries = [
        Entry(columns[0][i], columns[3][i], columns[2][i], columns[1][i]) for i in range(count)
    ]
    if any(entry.offset < 0 or not 0 < entry.length <= MAX_RANGE for entry in entries):
        raise MapError("Map source tile or directory range is invalid.")
    return columns[0], entries


class RangeSource:
    """No non-range fallback: a 200 response is rejected before reading its body."""

    def __init__(
        self,
        url: str,
        *,
        budget: int = MAX_DOWNLOAD_BYTES,
        etag: str | None = None,
        cancel: threading.Event | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not re.fullmatch(r"https://build\.protomaps\.com/\d{8}\.pmtiles", url):
            raise MapError("Unsupported map source. Use a published Protomaps daily build.")
        self.url, self.budget, self.etag = url, budget, etag
        self.received = 0
        self.total: int | None = None
        self.cancel = cancel or threading.Event()
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or not 0 < length <= MAX_RANGE:
            raise MapError("The requested map range exceeds its limit.")
        if self.total is not None and offset + length > self.total:
            raise MapError("The map source points outside its archive.")
        headers = {
            "Range": f"bytes={offset}-{offset + length - 1}",
            "Accept-Encoding": "identity",
            "User-Agent": "Outpost/0.1 regional offline map setup",
        }
        if self.etag is not None:
            headers["If-Match"] = self.etag
        for attempt in range(3):
            if self.cancel.is_set():
                raise MapError("Map download paused; resume when ready.")
            if self.received + length > self.budget:
                raise MapError(
                    "The map download reached its byte limit; reduce the area or detail."
                )
            try:
                with self.client.stream("GET", self.url, headers=headers) as response:
                    if response.status_code == 404:
                        raise MapError(
                            "This map source build is no longer available; prepare a new plan."
                        )
                    if response.status_code in {429, 500, 502, 503, 504}:
                        raise httpx.ReadError("Map source temporarily unavailable")
                    if (
                        response.status_code != 206
                        or response.headers.get("Content-Encoding", "identity") != "identity"
                    ):
                        raise MapError("Map source ignored the bounded range; download stopped.")
                    match = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
                    )
                    if not match or tuple(map(int, match.groups()[:2])) != (
                        offset,
                        offset + length - 1,
                    ):
                        raise MapError("The map source returned an incorrect byte range.")
                    total = int(match.group(3))
                    if total < offset + length or (self.total is not None and self.total != total):
                        raise MapError("The map source changed during setup; prepare a new plan.")
                    etag = response.headers.get("ETag", "")
                    if (
                        not etag
                        or not re.fullmatch(r'"[^"\x00-\x20\x7f]*"', etag)
                        or (self.etag is not None and etag != self.etag)
                    ):
                        raise MapError("The map source identity changed; prepare a new plan.")
                    self.total, self.etag = total, etag
                    result = bytearray()
                    for chunk in response.iter_raw(chunk_size=64 * 1024):
                        self.received += len(chunk)
                        if self.cancel.is_set():
                            raise MapError("Map download paused; resume when ready.")
                        if len(result) + len(chunk) > length or self.received > self.budget:
                            raise MapError("The map source exceeded its requested byte range.")
                        result.extend(chunk)
                    if len(result) != length:
                        raise httpx.ReadError("Incomplete range")
                    return bytes(result)
            except httpx.HTTPError as error:
                if attempt == 2:
                    raise MapError(
                        "Map download interrupted; check the connection and resume."
                    ) from error
                self.cancel.wait(2**attempt)
        raise MapError("Map download interrupted.")


class Archive:
    def __init__(self, read: Callable[[int, int], bytes]) -> None:
        self.read = read
        data = read(0, 127)
        if len(data) != 127 or data[:8] != b"PMTiles\x03":
            raise MapError("The map source is not a supported PMTiles archive.")
        sections = struct.unpack_from("<11Q", data, 8)
        self.root_offset, self.root_length = sections[:2]
        self.metadata_offset, self.metadata_length = sections[2:4]
        self.leaf_offset, self.leaf_length = sections[4:6]
        self.data_offset, self.data_length = sections[6:8]
        self.compression, self.tile_compression, kind = data[97:100]
        self.min_zoom, self.max_zoom = data[100:102]
        if kind != 1 or self.compression not in {1, 2} or self.tile_compression not in {1, 2}:
            raise MapError("This map source is not a supported vector basemap.")
        if not (127 <= self.root_offset < self.root_offset + self.root_length <= 16384):
            raise MapError("The map source root directory is invalid.")
        if not (0 < self.metadata_length <= 512 * 1024) or self.metadata_offset < 127:
            raise MapError("The map source metadata exceeds its supported limit.")
        if self.leaf_offset < 127 or self.data_offset < 127 or self.data_length <= 0:
            raise MapError("The map source sections are invalid.")
        self._directories: OrderedDict[tuple[int, int], tuple[list[int], list[Entry]]] = (
            OrderedDict()
        )
        self._directory(self.root_offset, self.root_length)
        raw = expand(
            read(self.metadata_offset, self.metadata_length), self.compression, 2 * 1024**2
        )
        try:
            metadata = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise MapError("The map source metadata is invalid.") from error
        if not isinstance(metadata, dict):
            raise MapError("The map source metadata is invalid.")
        self.metadata: dict[str, Any] = metadata

    def _directory(self, offset: int, length: int) -> tuple[list[int], list[Entry]]:
        key = offset, length
        if key not in self._directories:
            self._directories[key] = directory(self.read(offset, length), self.compression)
            if len(self._directories) > 16:
                self._directories.popitem(last=False)
        self._directories.move_to_end(key)
        return self._directories[key]

    def locate(self, identifier: int) -> tuple[int, int] | None:
        offset, length = self.root_offset, self.root_length
        seen: set[tuple[int, int]] = set()
        for _ in range(4):
            if (offset, length) in seen:
                raise MapError("The map source has a cyclic directory.")
            seen.add((offset, length))
            ids, entries = self._directory(offset, length)
            index = bisect.bisect_right(ids, identifier) - 1
            if index < 0:
                return None
            entry = entries[index]
            if entry.run:
                if identifier >= entry.identifier + entry.run:
                    return None
                if entry.offset + entry.length > self.data_length:
                    raise MapError("The map source has an invalid tile range.")
                return self.data_offset + entry.offset, entry.length
            if entry.offset + entry.length > self.leaf_length:
                raise MapError("The map source has an invalid directory range.")
            offset, length = self.leaf_offset + entry.offset, entry.length
        raise MapError("The map source exceeds the supported directory depth.")
