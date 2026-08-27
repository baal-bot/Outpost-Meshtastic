#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
from xml.sax.saxutils import escape

from outpost.config import load_config


def render(name: str, port: int) -> str:
    clean_name = " ".join(name.split()).replace("%", "%%") or "Outpost"
    display_name = escape(f"{clean_name} on %h")
    return f"""<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">{display_name}</name>
  <service>
    <type>_http._tcp</type>
    <port>{port}</port>
    <txt-record>path=/</txt-record>
    <txt-record>application=outpost</txt-record>
  </service>
</service-group>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Outpost's mDNS service declaration")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_name(f".{arguments.output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(render(config.node.name, config.web.port), encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, arguments.output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
