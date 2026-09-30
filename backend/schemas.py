"""Pydantic request bodies for the /api contract."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class NameBody(BaseModel):
    name: str
class BackfillBody(BaseModel):
    limit: int = 150

class MergeBody(BaseModel):
    target_id: int


class MoveFacesBody(BaseModel):
    face_ids: list[int]
    target_id: Optional[int] = None
    name: Optional[str] = None


class ReviewBody(BaseModel):
    decision: str  # yes | no


class ConfirmBody(BaseModel):
    confirm: str


class ExclusionBody(BaseModel):
    person_id: int
    media_id: int


class ParseSearchBody(BaseModel):
    query: str


class SeparateBody(BaseModel):
    person_a: int
    person_b: int


class LibraryBody(BaseModel):
    path: str
    ignored: Optional[list[str]] = None


class LibraryPatchBody(BaseModel):
    ignored: list[str]


class IndexBody(BaseModel):
    library_id: int
    force: bool = False
    retry_failed: bool = False


class SettingsPatch(BaseModel):
    matching_threshold: Optional[float] = None
    review_threshold: Optional[float] = None
    detection_size: Optional[int] = None
    video_interval: Optional[float] = None
    theme: Optional[str] = None
    multi_scale: Optional[bool] = None
    adaptive_video: Optional[bool] = None
    min_face_quality: Optional[float] = None
    auto_confirm: Optional[bool] = None
    auto_confirm_threshold: Optional[float] = None
    dino_similarity_threshold: Optional[float] = None


class MaintenanceBody(BaseModel):
    action: str
    confirm: str


class ExportBody(BaseModel):
    media_ids: Optional[list[int]] = None
    filters: Optional[dict] = None


class MoveMediaBody(BaseModel):
    destination: str
    media_ids: Optional[list[int]] = None  # if set, only these; else all media with this person's faces


class PurgeMediaBody(BaseModel):
    media_ids: list[int]
    confirm: str  # must be "DELETE"



class IgnoreDuplicateBody(BaseModel):
    media_ids: list[int]

