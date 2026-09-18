from math import asin, cos, radians, sin, sqrt
from typing import Protocol

SAVANNAH = (32.0809, -81.0912)
CITY_COORDS = {
    "savannah": (32.0809, -81.0912),
    "thunderbolt": (32.0341, -81.05),
    "pooler": (32.1155, -81.2471),
    "pembroke": (32.1356, -81.6222),
    "garden city": (32.1144, -81.1526),
    "port wentworth": (32.1491, -81.1632),
    "rincon": (32.2960, -81.2354),
    "richmond hill": (31.9383, -81.3034),
    "hinesville": (31.8469, -81.5959),
    "statesboro": (32.4488, -81.7832),
    "brunswick": (31.1499, -81.4915),
    "beaufort": (32.4316, -80.6698),
    "bluffton": (32.2371, -80.8604),
    "charleston": (32.7765, -79.9311),
    "north charleston": (32.8546, -79.9748),
    "jacksonville": (30.3322, -81.6557),
}
TIER_2 = {"north charleston"}
TIER_3 = {"charleston", "jacksonville"}
TIER_1 = set(CITY_COORDS) - TIER_2 - TIER_3


class TravelTimeProvider(Protocol):
    def minutes_from_savannah(self, latitude: float, longitude: float) -> int | None: ...


def geodesic_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
    value = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return round(3958.7613 * 2 * asin(sqrt(value)), 1)


def classify_territory(
    city: str | None, latitude: float | None, longitude: float | None
) -> tuple[str, float | None]:
    key = (city or "").strip().casefold()
    coords = (
        (latitude, longitude)
        if latitude is not None and longitude is not None
        else CITY_COORDS.get(key)
    )
    distance = geodesic_miles(SAVANNAH, coords) if coords is not None else None
    if distance is not None and distance > 160:
        return "OUTSIDE_TERRITORY", distance
    if key in TIER_1:
        return "TIER_1", distance
    if key in TIER_2:
        return "TIER_2", distance
    if key in TIER_3:
        return "TIER_3", distance
    return "OUTSIDE_TERRITORY", distance
