"""Capture date, GPS, camera and lens for photos and videos.

Date fallback chain: EXIF DateTimeOriginal/Digitized/DateTime (or the video's
creation time) -> a date in the file name (IMG_20190918_164153, PXL_..., 2021-06-01 12.30.00,
IMG-20200101-WA0001, Screenshot_2020-01-01-...) -> file modification time.
``date_source`` records which one was used so the UI can flag guessed dates.

EXIF wall-clock times have no zone; video creation times are UTC and are
converted to this machine's local time so both sort together sensibly.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

META_VERSION = 1
MIN_YEAR = 1990

_FILENAME_DATE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[ _T.-]?([01]\d|2[0-3])[-_.:h]?([0-5]\d)[-_.:m]?([0-5]\d)(?:\d{3})?)?(?!\d)"
)
_ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?")


def _plausible(value: datetime) -> bool:
    return MIN_YEAR <= value.year and value <= datetime.now() + timedelta(days=2)


def parse_exif_datetime(raw) -> Optional[str]:
    if not raw:
        return None
    text = str(raw).strip().strip("\x00")[:19]
    if not text or text.startswith("0000"):
        return None
    for fmt, length in (("%Y:%m:%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M:%S", 19), ("%Y:%m:%d", 10)):
        try:
            value = datetime.strptime(text[:length], fmt)
        except ValueError:
            continue
        return value.isoformat() if _plausible(value) else None
    return None


def date_from_filename(name: str) -> Optional[str]:
    for match in _FILENAME_DATE.finditer(Path(name).stem):
        year, month, day, hh, mm, ss = match.groups()
        try:
            # A date without a time is placed at noon so it sorts mid-day.
            value = datetime(int(year), int(month), int(day), int(hh) if hh else 12, int(mm or 0), int(ss or 0))
        except ValueError:
            continue
        if _plausible(value):
            return value.isoformat()
    return None


def _ratio(value) -> float:
    try:
        return float(value)
    except TypeError:
        num, den = value
        return float(num) / float(den) if den else 0.0


def gps_from_exif(gps: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    try:
        lat_parts, lon_parts = gps.get(2), gps.get(4)
        if not lat_parts or not lon_parts:
            return None, None, None
        lat = sum(_ratio(v) / (60 ** i) for i, v in enumerate(lat_parts))
        lon = sum(_ratio(v) / (60 ** i) for i, v in enumerate(lon_parts))
        if str(gps.get(1, "N")).upper().startswith("S"):
            lat = -lat
        if str(gps.get(3, "E")).upper().startswith("W"):
            lon = -lon
        alt = _ratio(gps[6]) if gps.get(6) is not None else None
        if alt is not None and gps.get(5) in (1, b"\x01"):
            alt = -alt
    except Exception:
        return None, None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None, None, None
    return round(lat, 7), round(lon, 7), alt


def parse_iso6709(text: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    match = _ISO6709.match((text or "").strip())
    if not match:
        return None, None, None
    lat, lon = float(match.group(1)), float(match.group(2))
    alt = float(match.group(3)) if match.group(3) else None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None, None, None
    return lat, lon, alt


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().strip("\x00").strip()
    return text[:120] or None


def _video_tags(path: Path) -> dict:
    from .media_processing import _ffprobe_bin

    ffprobe = _ffprobe_bin()
    if not ffprobe:
        return {}
    try:
        proc = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                              capture_output=True, text=True, timeout=30, check=False)
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return {}
    tags = {k.lower(): v for k, v in ((data.get("format") or {}).get("tags") or {}).items()}
    for stream in data.get("streams") or []:
        for k, v in (stream.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    return tags


def _utc_to_local(text: str) -> Optional[str]:
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() if _plausible(value) else None


def extract(path, kind: str, *, mtime_ns: Optional[int] = None) -> dict:
    path = Path(path)
    out = {"captured_at": None, "date_source": None, "gps_lat": None, "gps_lon": None, "gps_alt": None,
           "camera_make": None, "camera_model": None, "lens": None, "meta_version": META_VERSION}
    if kind == "photo":
        from . import imaging

        tags = imaging.exif(path)
        out["captured_at"] = (parse_exif_datetime(tags.get(36867)) or parse_exif_datetime(tags.get(36868))
                              or parse_exif_datetime(tags.get(306)))
        out["camera_make"], out["camera_model"] = _clean(tags.get(271)), _clean(tags.get(272))
        out["lens"] = _clean(tags.get(42036))
        out["gps_lat"], out["gps_lon"], out["gps_alt"] = gps_from_exif(tags.get("gps") or {})
    else:
        tags = _video_tags(path)
        created = (tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time") or tags.get("date"))
        if created:
            out["captured_at"] = _utc_to_local(created)
        location = tags.get("com.apple.quicktime.location.iso6709") or tags.get("location") or tags.get("location-eng")
        if location:
            out["gps_lat"], out["gps_lon"], out["gps_alt"] = parse_iso6709(location)
        out["camera_make"] = _clean(tags.get("com.apple.quicktime.make") or tags.get("make"))
        out["camera_model"] = _clean(tags.get("com.apple.quicktime.model") or tags.get("model"))
    if out["captured_at"]:
        out["date_source"] = "exif" if kind == "photo" else "container"
    else:
        guessed = date_from_filename(path.name)
        if guessed:
            out["captured_at"], out["date_source"] = guessed, "filename"
        else:
            try:
                ns = mtime_ns if mtime_ns else path.stat().st_mtime_ns
                out["captured_at"] = datetime.fromtimestamp(ns / 1e9).replace(microsecond=0).isoformat()
                out["date_source"] = "mtime"
            except OSError:
                pass
    return out


COLUMNS = ("captured_at", "date_source", "gps_lat", "gps_lon", "gps_alt", "camera_make", "camera_model", "lens",
           "meta_version")

