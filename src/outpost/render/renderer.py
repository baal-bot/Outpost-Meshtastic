from __future__ import annotations

from outpost.router.models import Response


def render_response(response: Response) -> str:
    return "\n".join(line.text for line in response.lines)
