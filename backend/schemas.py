"""Pydantic request bodies for the /api contract."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


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
    embed: bool = False  # include the SigLIP text vector in the response


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
    map_tiles_enabled: Optional[bool] = None
    map_pmtiles_path: Optional[str] = None


class MaintenanceBody(BaseModel):
    action: str
    confirm: str


class ExportBody(BaseModel):
    media_ids: Optional[list[int]] = None
    filters: Optional[dict] = None
    apply_edits: bool = False  # export edited photos as rendered JPEGs instead of the originals


class MoveMediaBody(BaseModel):
    destination: str
    media_ids: Optional[list[int]] = None  # if set, only these; else all media with this person's faces


class PurgeMediaBody(BaseModel):
    media_ids: list[int]
    confirm: str  # must be "DELETE"



class IgnoreDuplicateBody(BaseModel):
    media_ids: list[int]



class HybridSearchBody(BaseModel):
    text: Optional[str] = None
    people: list[int] = Field(default_factory=list)
    people_mode: Literal["ANY", "ALL"] = "ANY"
    exclude_people: list[int] = Field(default_factory=list)
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    kind: Optional[Literal["photo", "video"]] = None
    min_quality: Optional[float] = Field(None, ge=0, le=1)
    name: Optional[str] = None
    deleted: bool = False
    similar_media_id: Optional[int] = None
    similar_face_id: Optional[int] = None
    weights: dict[str, float] = Field(default_factory=dict)
    page: int = Field(1, ge=1)
    limit: int = Field(60, ge=1, le=200)


class SavedSearchBody(BaseModel):
    name: str
    query: HybridSearchBody


class SavedSearchRunBody(BaseModel):
    page: int = Field(1, ge=1)
    limit: int = Field(60, ge=1, le=200)


class JobBody(BaseModel):
    kind: str
    payload: dict = Field(default_factory=dict)
    priority: int = Field(50, ge=0, le=100)


class IntegrityCheckBody(BaseModel):
    verify_sample: int = Field(5000, ge=100, le=10_000_000)


class BackupExportBody(BaseModel):
    include_thumbnails: bool = False


class BackupRestoreBody(BaseModel):
    name: str


class EventRenameBody(BaseModel):
    name: str


class EventMergeBody(BaseModel):
    event_ids: list[int]


class EventSplitBody(BaseModel):
    media_id: int


class EventMoveBody(BaseModel):
    media_ids: list[int]


class EventDetectBody(BaseModel):
    full: bool = False


class MomentSearchBody(BaseModel):
    text: str
    limit: int = Field(40, ge=1, le=200)
    people: list[int] = Field(default_factory=list)


class PersonClipsBody(BaseModel):
    person_id: int
    precise: bool = False


class AlbumBody(BaseModel):
    name: str
    media_ids: list[int] = Field(default_factory=list)


class AlbumItemsBody(BaseModel):
    media_ids: list[int]


class FavoriteBody(BaseModel):
    media_ids: list[int]
    favorite: bool = True


class MediaBatchBody(BaseModel):
    media_ids: list[int]


class CollectionBody(BaseModel):
    name: str
    query: HybridSearchBody


class SplitBody(BaseModel):
    face_ids: list[int]
    name: Optional[str] = None


class DuplicateGroupDecision(BaseModel):
    keep: list[int] = Field(default_factory=list)
    remove: list[int]


class ResolveDuplicatesBody(BaseModel):
    groups: list[DuplicateGroupDecision]
    free_space: bool = False


class CropBody(BaseModel):
    x: float
    y: float
    w: float
    h: float


class EditPatchBody(BaseModel):
    rotation: Optional[int] = None
    flip_h: Optional[bool] = None
    flip_v: Optional[bool] = None
    crop: Optional[CropBody] = None
    rating: Optional[int] = None
    label: Optional[str] = None
    flag: Optional[str] = None


class EditRevertBody(BaseModel):
    history_id: Optional[int] = None


class EditBatchBody(BaseModel):
    media_ids: list[int]
    rating: Optional[int] = None
    label: Optional[str] = None
    flag: Optional[str] = None


class ShareDestination(BaseModel):
    type: Literal["zip", "folder"] = "zip"
    path: Optional[str] = None


class ShareOptions(BaseModel):
    mode: Literal["all_except_kept", "only_selected"] = "all_except_kept"
    keep_people: list[int] = Field(default_factory=list)
    anonymize_people: list[int] = Field(default_factory=list)
    anonymize_unknown: bool = True
    method: Literal["blur", "pixelate", "mask"] = "blur"
    strength: float = Field(0.7, ge=0, le=1)
    strip_metadata: bool = True
    strip_gps: bool = True
    apply_edits: bool = True
    include_videos: bool = False
    verify: bool = True
    destination: ShareDestination = Field(default_factory=ShareDestination)


class SharePeopleBody(BaseModel):
    media_ids: list[int]


class SharePreviewBody(BaseModel):
    media_id: int
    options: ShareOptions = Field(default_factory=ShareOptions)


class ShareBody(BaseModel):
    media_ids: list[int]
    options: ShareOptions = Field(default_factory=ShareOptions)
