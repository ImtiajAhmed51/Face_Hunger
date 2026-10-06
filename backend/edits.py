"""Non-destructive edit model: geometry, rendering and XMP sidecar read/write (pure functions).

An edit never touches the original. It is a small record:

  rotation   0 / 90 / 180 / 270 degrees clockwise, applied first
  flip_h     mirror left-right, applied after the rotation
  flip_v     mirror top-bottom, applied after the rotation
  crop       {x, y, w, h} as fractions (0-1) of the rotated/flipped frame
  rating     0-5        label  Red|Yellow|Green|Blue|Purple        flag  pick|reject

The XMP sidecar stores what other tools understand: ``xmp:Rating``, ``xmp:Label``,
``tiff:Orientation`` (the rotation/flip as an EXIF orientation 1-8, i.e. the orientation
override) and the Camera Raw crop (``crs:HasCrop``, ``crs:CropLeft/Top/Right/Bottom``, here
relative to the oriented frame). The flag has no standard XMP field, so it goes into our own
namespace and the JSON sidecar. Existing sidecars written by other applications are parsed
and updated in place: properties we do not own are preserved.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from typing import Optional

import numpy as np

LABELS = ("Red", "Yellow", "Green", "Blue", "Purple")
FLAGS = ("pick", "reject")
ASPECTS = {"free": None, "original": "original", "1:1": 1.0, "4:3": 4 / 3, "3:2": 3 / 2, "16:9": 16 / 9,
           "3:4": 3 / 4, "2:3": 2 / 3, "9:16": 9 / 16}
EMPTY = {"rotation": 0, "flip_h": False, "flip_v": False, "crop": None, "rating": 0, "label": None, "flag": None}

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "fh": "https://facehunger.app/ns/1.0/",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)


def _q(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


class EditError(ValueError):
    pass


def normalize(edit: dict) -> dict:
    """Validate and canonicalise an edit record (raises EditError)."""
    out = dict(EMPTY)
    out.update({k: v for k, v in (edit or {}).items() if k in EMPTY})
    try:
        out["rotation"] = int(out["rotation"] or 0) % 360
    except (TypeError, ValueError) as exc:
        raise EditError("rotation must be a number") from exc
    if out["rotation"] % 90:
        raise EditError("rotation must be a multiple of 90")
    out["flip_h"], out["flip_v"] = bool(out["flip_h"]), bool(out["flip_v"])
    try:
        out["rating"] = int(out["rating"] or 0)
    except (TypeError, ValueError) as exc:
        raise EditError("rating must be 0-5") from exc
    if not 0 <= out["rating"] <= 5:
        raise EditError("rating must be 0-5")
    if out["label"] is not None:
        match = next((name for name in LABELS if name.lower() == str(out["label"]).lower()), None)
        if match is None:
            raise EditError(f"label must be one of {', '.join(LABELS)}")
        out["label"] = match
    if out["flag"] is not None and out["flag"] not in FLAGS:
        raise EditError("flag must be pick or reject")
    crop = out["crop"]
    if crop is not None:
        try:
            x, y, w, h = (float(crop[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError) as exc:
            raise EditError("crop needs x, y, w, h") from exc
        x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        w, h = min(w, 1.0 - x), min(h, 1.0 - y)
        if w < 0.01 or h < 0.01:
            raise EditError("crop is too small")
        full = x < 1e-4 and y < 1e-4 and w > 1 - 1e-4 and h > 1 - 1e-4
        out["crop"] = None if full else {"x": round(x, 6), "y": round(y, 6), "w": round(w, 6), "h": round(h, 6)}
    return out


def is_identity_geometry(edit: dict) -> bool:
    return not (edit.get("rotation") or edit.get("flip_h") or edit.get("flip_v") or edit.get("crop"))


def _transform_array(a: np.ndarray, rotation: int, flip_h: bool, flip_v: bool) -> np.ndarray:
    a = np.rot90(a, k=-(rotation // 90) % 4)  # clockwise
    if flip_h:
        a = a[:, ::-1]
    if flip_v:
        a = a[::-1, :]
    return a


def _orientation_table() -> dict:
    """(rotation, mirrored) <-> EXIF orientation, derived by brute force so it cannot drift."""
    probe = np.arange(6).reshape(2, 3)
    exif_ops = {1: lambda a: a, 2: lambda a: a[:, ::-1], 3: lambda a: a[::-1, ::-1], 4: lambda a: a[::-1, :],
                5: lambda a: a.T, 6: lambda a: np.rot90(a, -1), 7: lambda a: np.rot90(a, 2).T, 8: lambda a: np.rot90(a, 1)}
    table = {}
    for rotation in (0, 90, 180, 270):
        for mirrored in (False, True):
            ours = _transform_array(probe, rotation, mirrored, False)
            for code, op in exif_ops.items():
                theirs = op(probe)
                if theirs.shape == ours.shape and (theirs == ours).all():
                    table[(rotation, mirrored)] = code
    return table


_TO_EXIF = _orientation_table()
_FROM_EXIF = {v: k for k, v in _TO_EXIF.items()}


def to_exif_orientation(edit: dict) -> int:
    """The rotation/flips as one EXIF orientation value (a vertical flip is a mirror plus 180 degrees)."""
    probe = np.arange(6).reshape(2, 3)
    ours = _transform_array(probe, int(edit.get("rotation") or 0), bool(edit.get("flip_h")), bool(edit.get("flip_v")))
    for (rotation, mirrored), code in _TO_EXIF.items():
        candidate = _transform_array(probe, rotation, mirrored, False)
        if candidate.shape == ours.shape and (candidate == ours).all():
            return code
    return 1


def from_exif_orientation(code: int) -> dict:
    rotation, mirrored = _FROM_EXIF.get(int(code), (0, False))
    return {"rotation": rotation, "flip_h": mirrored, "flip_v": False}


def apply(image, edit: dict):
    """Render an edit onto an upright PIL image and return a new image."""
    from PIL import Image

    rotation = int(edit.get("rotation") or 0)
    if rotation:
        image = image.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                                 270: Image.Transpose.ROTATE_90}[rotation])
    if edit.get("flip_h"):
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if edit.get("flip_v"):
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    crop = edit.get("crop")
    if crop:
        w, h = image.size
        box = (round(crop["x"] * w), round(crop["y"] * h), round((crop["x"] + crop["w"]) * w), round((crop["y"] + crop["h"]) * h))
        box = (box[0], box[1], max(box[0] + 1, box[2]), max(box[1] + 1, box[3]))
        image = image.crop(box)
    return image


def output_size(width: int, height: int, edit: dict) -> tuple[int, int]:
    if int(edit.get("rotation") or 0) in (90, 270):
        width, height = height, width
    crop = edit.get("crop")
    if crop:
        width, height = max(1, round(width * crop["w"])), max(1, round(height * crop["h"]))
    return width, height


# ---------------------------------------------------------------------------
# XMP
# ---------------------------------------------------------------------------

def _description(root: ET.Element) -> ET.Element:
    rdf = root if root.tag == _q("rdf", "RDF") else root.find(f".//{_q('rdf', 'RDF')}")
    if rdf is None:
        raise EditError("Not an XMP packet (no rdf:RDF)")
    desc = rdf.find(_q("rdf", "Description"))
    if desc is None:
        desc = ET.SubElement(rdf, _q("rdf", "Description"), {_q("rdf", "about"): ""})
    return desc


def _get(root: ET.Element, prefix: str, name: str) -> Optional[str]:
    """A simple property in either attribute or element form, on any rdf:Description."""
    key = _q(prefix, name)
    for desc in root.iter(_q("rdf", "Description")):
        if key in desc.attrib:
            return desc.attrib[key]
        child = desc.find(key)
        if child is not None and child.text is not None:
            return child.text.strip()
    return None


def _set(root: ET.Element, prefix: str, name: str, value: Optional[str]) -> None:
    key = _q(prefix, name)
    for desc in root.iter(_q("rdf", "Description")):
        desc.attrib.pop(key, None)
        for child in desc.findall(key):
            desc.remove(child)
    if value is not None:
        _description(root).set(key, value)


def parse_xmp(data: bytes) -> dict:
    """Read the fields we understand from an XMP sidecar (ours or another application's)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise EditError(f"Invalid XMP: {exc}") from exc
    _description(root)
    edit = dict(EMPTY)
    rating = _get(root, "xmp", "Rating")
    if rating is not None:
        try:
            value = int(float(rating))
        except ValueError:
            value = 0
        if value < 0:  # XMP convention: -1 means rejected
            edit["flag"] = "reject"
        edit["rating"] = min(5, max(0, value))
    label = _get(root, "xmp", "Label")
    if label:
        edit["label"] = next((name for name in LABELS if name.lower() == label.strip().lower()), None)
    orientation = _get(root, "tiff", "Orientation")
    if orientation and orientation.strip().isdigit():
        edit.update(from_exif_orientation(int(orientation)))
    if (_get(root, "crs", "HasCrop") or "").lower() == "true":
        try:
            left, top, right, bottom = (float(_get(root, "crs", n)) for n in ("CropLeft", "CropTop", "CropRight", "CropBottom"))
            edit["crop"] = {"x": left, "y": top, "w": right - left, "h": bottom - top}
        except (TypeError, ValueError):
            edit["crop"] = None
    flag = _get(root, "fh", "Flag")
    if flag in FLAGS:
        edit["flag"] = flag
    return normalize(edit)


def write_xmp(edit: dict, existing: Optional[bytes] = None) -> bytes:
    """Serialise an edit; with ``existing`` the other application's properties are kept."""
    edit = normalize(edit)
    root = None
    if existing:
        try:
            root = ET.fromstring(existing)
            _description(root)
        except (ET.ParseError, EditError):
            root = None
    if root is None:
        root = ET.Element(_q("x", "xmpmeta"), {_q("x", "xmptk"): "Face Hunger"})
        rdf = ET.SubElement(root, _q("rdf", "RDF"))
        ET.SubElement(rdf, _q("rdf", "Description"), {_q("rdf", "about"): ""})
    _set(root, "xmp", "Rating", str(edit["rating"]) if edit["rating"] else None)
    _set(root, "xmp", "Label", edit["label"])
    orientation = to_exif_orientation(edit)
    _set(root, "tiff", "Orientation", str(orientation) if orientation != 1 else None)
    crop = edit["crop"]
    _set(root, "crs", "HasCrop", "True" if crop else None)
    for name, value in (("CropLeft", crop and crop["x"]), ("CropTop", crop and crop["y"]),
                        ("CropRight", crop and crop["x"] + crop["w"]), ("CropBottom", crop and crop["y"] + crop["h"]),
                        ("CropAngle", crop and 0)):
        _set(root, "crs", name, None if not crop else f"{value:.6f}".rstrip("0").rstrip(".") or "0")
    _set(root, "fh", "Flag", edit["flag"])
    body = ET.tostring(root, encoding="unicode")
    packet = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n' + body + '\n<?xpacket end="w"?>\n')
    return packet.encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
