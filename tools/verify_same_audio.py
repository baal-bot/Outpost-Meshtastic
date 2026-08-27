#!/usr/bin/env python3
"""Verify the pinned upstream SAME audio fixture against the installed decoder."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

FIXTURE_URL = (
    "https://raw.githubusercontent.com/cbs228/sameold/samedec-0.4.2/sample/npt.22050.s16le.bin"
)
FIXTURE_SHA256 = "65c58a6c3e34fa5ed68f7288b6f10369bfb73034e5f12da5e8e63671dcf15b88"
EXPECTED_HEADER = "ZCZC-PEP-NPT-000000+0030-2771820-TEST    -"
MAX_FIXTURE_BYTES = 400_000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decoder", default="samedec")
    args = parser.parse_args()
    decoder = shutil.which(args.decoder) if "/" not in args.decoder else args.decoder
    if not decoder or not Path(decoder).is_file():
        raise SystemExit(f"decoder is unavailable: {args.decoder}")

    request = urllib.request.Request(FIXTURE_URL, headers={"User-Agent": "Outpost acceptance"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = response.read(MAX_FIXTURE_BYTES + 1)
    if len(payload) > MAX_FIXTURE_BYTES:
        raise SystemExit("fixture exceeds the bounded download size")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != FIXTURE_SHA256:
        raise SystemExit(f"fixture checksum mismatch: {digest}")

    with tempfile.NamedTemporaryFile(suffix=".22050.s16le.bin") as fixture:
        fixture.write(payload)
        fixture.flush()
        result = subprocess.run(  # noqa: S603
            [decoder, "--rate", "22050", "--file", fixture.name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    headers = [
        line.strip("\r\n") for line in result.stdout.splitlines() if line.startswith("ZCZC-")
    ]
    if result.returncode or headers != [EXPECTED_HEADER]:
        raise SystemExit(
            f"decoder fixture failed (status={result.returncode}, headers={headers!r}, "
            f"stderr={result.stderr[-300:]!r})"
        )
    print(f"SAME audio fixture decoded successfully: {headers[0]}")


if __name__ == "__main__":
    main()
