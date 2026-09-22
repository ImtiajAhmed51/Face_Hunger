export interface Person {
  id: number;
  name: string | null;
  display_name: string;
  face_count: number;
  photo_count: number;
  video_count: number;
  representative_face_id: number | null;
  unreviewed_count: number;
  sample_face_ids?: number[];
}
export interface Media {
  id: number;
  name: string;
  path?: string;
  kind: "photo" | "video";
  captured_at: string | null;
  width: number | null;
  height: number | null;
  duration: number | null;
  size: number;
  status: string;
  missing: boolean;
  deleted_at: string | null;
  face_count: number;
  people: { id: number; display_name: string }[];
  similarity?: number;
  playback_status?: string;
  conversion?: {
    status: string;
    progress: number;
    stage: string;
    ready: boolean;
    error?: string | null;
    indeterminate?: boolean;
    mode?: string | null;
  };
  error?: string | null;
  /** Soft-kept pre-conversion original on disk */
  has_original?: boolean;
  original_path?: string | null;
  original_name?: string | null;
}
export interface Face {
  id: number;
  media_id: number;
  person_id: number | null;
  display_name: string;
  bbox: [number, number, number, number];
  timestamp: number | null;
  detection: number;
  similarity: number | null;
  confidence_label: string;
  review_state: "confirmed" | "unreviewed" | "rejected";
  deleted_at: string | null;
  excluded: boolean;
}
export interface MediaDetail extends Media {
  faces: Face[];
}
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  limit: number;
}
export interface Engine {
  state: "unloaded" | "loading" | "ready" | "error";
  provider: string | null;
  available_providers: string[];
  model: "buffalo_l";
  error: string | null;
}
export interface Job {
  id: number;
  library_id: number | null;
  status:
    | "queued"
    | "running"
    | "paused"
    | "completed"
    | "cancelled"
    | "failed"
    | "interrupted";
  phase: string;
  total: number;
  processed: number;
  faces: number;
  people: number;
  skipped: number;
  failed: number;
  current_file: string | null;
  error: string | null;
}
export interface SoftOriginal {
  media_id: number;
  media_name: string | null;
  converted_path?: string | null;
  original_path: string;
  original_name: string;
  size: number;
  kind: string;
}
export interface ConvertedBackup {
  path: string;
  name: string;
  size: number;
  media_id: number | null;
  media_name: string | null;
}
export interface CleanupCounts {
  duplicates: number;
  low_confidence: number;
  unreviewed: number;
  failed: number;
  missing: number;
  deleted_faces: number;
  soft_originals?: number;
  converted_backups?: number;
}

export interface Cleanup extends Omit<CleanupCounts, "soft_originals" | "converted_backups"> {
  possible_people: { a: Person; b: Person; similarity: number }[];
  soft_originals_list?: SoftOriginal[];
  soft_originals?: SoftOriginal[] | number;
  converted_backups?: ConvertedBackup[] | number;
  failed_media: Media[];
  missing_media: Media[];
  embedding_errors: number | null;
}
export interface Dashboard {
  photos: number;
  videos: number;
  faces: number;
  people: number;
  review_count: number;
  cleanup: CleanupCounts;
  recent_people: Person[];
  recent_media: Media[];
  job: Job | null;
  engine: Engine;
}
export interface Library {
  id: number;
  name: string;
  path: string;
  ignored: string[];
  media_count: number;
}
export type Theme = "light" | "dark" | "system";
export interface Settings {
  matching_threshold: number;
  review_threshold: number;
  detection_size: 320 | 640 | 960;
  video_interval: number;
  theme: Theme;
  dino_similarity_threshold?: number;
  storage: { database: number; embeddings: number; thumbnails: number };
  roots: string[];
}
export interface Exclusion {
  person_id: number;
  media_id: number;
  display_name: string;
  name: string;
}
export interface MediaFilters {
  kind?: "photo" | "video" | "";
  people?: number[];
  exclude_people?: number[];
  mode?: "ANY" | "ALL";
  date_from?: string;
  date_to?: string;
  confidence?: number | "";
  reviewed?: "confirmed" | "unreviewed" | "";
  excluded?: boolean;
  deleted?: boolean;
  no_faces?: boolean;
  sort?: "date" | "size_desc" | "size_asc" | "name" | "";
  q?: string;
}
export interface ParsedSearch {
  people: number[];
  mode: "ANY" | "ALL";
  date_from?: string;
  date_to?: string;
  kind?: "photo" | "video";
  unmatched: string[];
}
