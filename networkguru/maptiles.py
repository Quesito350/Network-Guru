"""Map backgrounds for the Map page (Admin → Map). The viewer's browser loads the tiles itself -
the server never does - and the page's Content-Security-Policy lets in only the chosen provider.
Offline, the page falls back to a plain grid: the pins are placed by GPS either way.

Check that ASI's use fits the provider's terms (Esri: an ArcGIS licence may be needed); otherwise
choose Street or None.
"""

from __future__ import annotations

TILES: dict[str, dict | None] = {
    "satellite": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "origin": "https://server.arcgisonline.com",
        "max_zoom": 19,
        "attribution": "Imagery © Esri, Maxar, Earthstar Geographics and the GIS User Community",
        "referrer": "no-referrer",
    },
    "street": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "origin": "https://tile.openstreetmap.org",
        "max_zoom": 19,
        "attribution": "© OpenStreetMap contributors",
        "referrer": "strict-origin-when-cross-origin",      # OpenStreetMap's tile policy asks for one
    },
    "none": None,
}
DEFAULT = "satellite"


def choice(value) -> str:
    return value if value in TILES else DEFAULT


def tiles_for(value) -> dict | None:
    """What the browser needs: the URL template, zoom limit, attribution and referrer policy."""
    t = TILES[choice(value)]
    return None if t is None else {k: t[k] for k in ("url", "max_zoom", "attribution", "referrer")}


def img_origin(value) -> str:
    """The extra img-src for the Content-Security-Policy ('' for None)."""
    t = TILES[choice(value)]
    return t["origin"] if t else ""
