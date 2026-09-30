"""Timeline buckets, map points and the optional offline PMTiles basemap."""

from __future__ import annotations

import gzip
import json
import struct
from pathlib import Path
from typing import Optional

DAY_EXPR = "substr(COALESCE(m.captured_at, m.indexed_at), 1, 10)"
TILE_TYPES = {1: "mvt", 2: "png", 3: "jpg", 4: "webp", 5: "avif"}


def timeline_days(db, kind: Optional[str] = None) -> dict:
    where, params = ["m.deleted_at IS NULL"], []
    if kind in ("photo", "video"):
        where.append("m.kind = ?")
        params.append(kind)
    rows = db.all(f"SELECT {DAY_EXPR} AS day, COUNT(*) AS n, "
                  "SUM(CASE WHEN m.date_source IN ('mtime','filename') THEN 1 ELSE 0 END) AS guessed "
                  f"FROM media m WHERE {' AND '.join(where)} GROUP BY day ORDER BY day DESC", tuple(params))
    days = [[r["day"], r["n"]] for r in rows if r["day"]]
    undated = sum(r["n"] for r in rows if not r["day"])
    return {"days": days, "total": sum(d[1] for d in days) + undated, "undated": undated,
            "guessed": sum(int(r["guessed"] or 0) for r in rows)}


def map_points(db, kind: Optional[str] = None) -> dict:
    where, params = ["deleted_at IS NULL", "gps_lat IS NOT NULL", "gps_lon IS NOT NULL"], []
    if kind in ("photo", "video"):
        where.append("kind = ?")
        params.append(kind)
    rows = db.all(f"SELECT id, gps_lat, gps_lon, kind FROM media WHERE {' AND '.join(where)}", tuple(params))
    # Columnar and rounded to ~1 m: 50k points is ~1.5 MB of JSON, parsed in a few ms.
    return {"count": len(rows), "ids": [r["id"] for r in rows],
            "lat": [round(r["gps_lat"], 5) for r in rows], "lon": [round(r["gps_lon"], 5) for r in rows],
            "video": [1 if r["kind"] == "video" else 0 for r in rows]}


def pmtiles_info(path: Path) -> dict:
    """Parse a PMTiles v3 header (+ metadata) without any dependency."""
    with path.open("rb") as handle:
        header = handle.read(127)
        if len(header) < 127 or header[:7] != b"PMTiles" or header[7] != 3:
            raise ValueError("Not a PMTiles v3 file")
        (meta_off, meta_len) = struct.unpack_from("<QQ", header, 24)
        internal_compression = header[97]
        tile_type = header[99]
        min_zoom, max_zoom = header[100], header[101]
        min_lon, min_lat, max_lon, max_lat = (v / 1e7 for v in struct.unpack_from("<iiii", header, 102))
        handle.seek(meta_off)
        raw = handle.read(min(meta_len, 4 << 20))
    try:
        metadata = json.loads(gzip.decompress(raw) if internal_compression == 2 else raw)
    except Exception:
        metadata = {}
    layers = [layer.get("id") for layer in metadata.get("vector_layers") or [] if layer.get("id")]
    return {"tile_type": TILE_TYPES.get(tile_type, "unknown"), "min_zoom": min_zoom, "max_zoom": max_zoom,
            "bounds": [min_lon, min_lat, max_lon, max_lat], "vector_layers": layers,
            "attribution": metadata.get("attribution"), "name": metadata.get("name")}
