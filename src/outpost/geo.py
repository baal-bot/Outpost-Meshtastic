from __future__ import annotations

import math


def distance_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, int]:
    """Return great-circle distance in kilometres and initial bearing in degrees."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lon / 2) ** 2
    )
    distance = 6371.0088 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))
    y = math.sin(delta_lon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lon)
    return distance, round((math.degrees(math.atan2(y, x)) + 360) % 360)
