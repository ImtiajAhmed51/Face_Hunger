"""Local-only InsightFace detection and recognition; never download model files.

Supports single-scale and multi-scale detection. Multi-scale runs the detector
at several input sizes, merges boxes with NMS, and attaches a quality score
(pose + blur + det confidence) used downstream for matching and review.

On Apple Silicon, prefers CoreMLExecutionProvider when
available in the installed onnxruntime build.
"""

import hashlib
import json
import logging
import os
import platform
import threading
from pathlib import Path

import numpy as np

from .embeddings import DIMENSION, normalize
from .quality import face_quality

logger = logging.getLogger(__name__)


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    intersection = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(
        0, min(ay + ah, by + bh) - max(ay, by))
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


class Detections(list):
    def __init__(self, items=(), duplicate_count=0):
        super().__init__(items)
        self.duplicate_count = duplicate_count


def deduplicate(detections, threshold=0.6):
    kept = Detections(duplicate_count=getattr(detections, "duplicate_count", 0))
    for face in sorted(detections, key=lambda f: (f.get("quality", f["detection"]), f["detection"]), reverse=True):
        if any(iou(face["bbox"], other["bbox"]) >= threshold for other in kept):
            kept.duplicate_count += 1
        else:
            kept.append(face)
    return kept


# Multi-scale presets: small faces / far subjects need higher res; bulk indexing can stay fast.
MULTI_SCALE_SIZES = (320, 640, 960)


def _provider_label(provider: str | None) -> str:
    if not provider:
        return "unloaded"
    if provider == "CoreMLExecutionProvider":
        return "Apple CoreML (CPU fallback allowed)"
    if provider == "CUDAExecutionProvider":
        return "CUDA"
    if provider == "DmlExecutionProvider":
        return "DirectML"
    if provider == "CPUExecutionProvider":
        return "CPU"
    return provider


class Engine:
    def __init__(self, config):
        self.model_dir = Path(config.model_dir) / "buffalo_l"
        self.data_dir = Path(config.data_dir)
        self.detection_threshold = float(getattr(config, "face_detection_threshold", 0.6))
        self.min_face_size = int(getattr(config, "face_min_size", 24))
        self.inference_threads = int(getattr(config, "face_inference_threads", 2))
        self.provider_preference = getattr(config, "face_provider", "auto")
        self._metadata = None
        self._lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._state = "unloaded"
        self._provider = None
        self._error = None
        self._detector = self._recognizer = None
        self.detection_size = 640
        self.multi_scale = False  # when True, detect at 320+640+960 and NMS-merge

    def status(self):
        try:
            import onnxruntime as ort
            available = list(ort.get_available_providers())
        except Exception:
            available = []
        with self._status_lock:
            return {
                "state": self._state,
                "provider": self._provider,
                "provider_label": _provider_label(self._provider),
                "available_providers": available,
                "model": "buffalo_l",
                "error": self._error,
                "detection_threshold": self.detection_threshold,
                "min_face_size": self.min_face_size,
                "metadata": self._metadata,
                "multi_scale": self.multi_scale,
                "detection_size": self.detection_size,
            }

    def configure(self, detection_size=None, multi_scale=None):
        with self._lock:
            if detection_size is not None:
                if detection_size not in (320, 640, 960):
                    raise ValueError("detection_size must be 320, 640, or 960")
                self.detection_size = int(detection_size)
                if self._detector is not None:
                    self._detector.input_size = (self.detection_size, self.detection_size)
            if multi_scale is not None:
                self.multi_scale = bool(multi_scale)

    def load(self):
        with self._lock:
            if self._detector is not None and self._recognizer is not None and self._state == "ready":
                return self.status()
            self._detector = self._recognizer = None
            with self._status_lock:
                self._state, self._error = "loading", None
            try:
                files = [self.model_dir / "det_10g.onnx", self.model_dir / "w600k_r50.onnx"]
                missing = [str(p) for p in files if not p.is_file() or not p.stat().st_size]
                if missing:
                    raise FileNotFoundError(
                        "Install buffalo_l locally. Missing: " + ", ".join(missing)
                        + ". Automatic model downloads are disabled."
                    )
                import onnxruntime as ort
                # InsightFace imports Albumentations; disable its online version check too.
                os.environ["NO_ALBUMENTATIONS_UPDATE"] = "1"
                from insightface.model_zoo import get_model

                ort.disable_telemetry_events()
                available = ort.get_available_providers()
                # On Apple Silicon prefer CoreML (GPU / Neural Engine) before CUDA/CPU.
                # CUDA is irrelevant on M-series; CoreML is the supported Apple path.
                is_apple = platform.system() == "Darwin" and platform.machine() in (
                    "arm64",
                    "aarch64",
                )
                if is_apple:
                    preferred_order = (
                        "CoreMLExecutionProvider",
                        "CPUExecutionProvider",
                    )
                else:
                    preferred_order = (
                        "CUDAExecutionProvider",
                        "CoreMLExecutionProvider",
                        "DmlExecutionProvider",
                        "CPUExecutionProvider",
                    )
                if self.provider_preference == "cpu":
                    preferred_order = ("CPUExecutionProvider",)
                elif self.provider_preference == "coreml":
                    preferred_order = ("CoreMLExecutionProvider", "CPUExecutionProvider")
                candidates = [p for p in preferred_order if p in available]
                errors = []
                for provider in candidates:
                    try:
                        options = ort.SessionOptions()
                        options.intra_op_num_threads = self.inference_threads
                        options.inter_op_num_threads = 1
                        if provider == "DmlExecutionProvider":
                            options.enable_mem_pattern = False
                            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                        # CoreML often benefits from sequential execution for stability
                        if provider == "CoreMLExecutionProvider":
                            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                        providers = [provider] + (
                            ["CPUExecutionProvider"] if provider != "CPUExecutionProvider" else []
                        )
                        detector, recognizer = [
                            get_model(
                                str(p.resolve()),
                                download=False,
                                providers=providers,
                                sess_options=options,
                            )
                            for p in files
                        ]
                        if detector is None or recognizer is None:
                            raise RuntimeError("Unrecognized buffalo_l ONNX models")
                        if detector.taskname != "detection" or recognizer.taskname != "recognition":
                            raise RuntimeError("Incorrect detection/recognition model files")
                        ctx = -1 if provider == "CPUExecutionProvider" else 0
                        detector.prepare(
                            ctx_id=ctx,
                            input_size=(self.detection_size,) * 2,
                            det_thresh=self.detection_threshold,
                        )
                        recognizer.prepare(ctx_id=ctx)
                        actual = [
                            model.session.get_providers()[0]
                            for model in (detector, recognizer)
                        ]
                        if actual != [provider, provider]:
                            raise RuntimeError(
                                f"Requested {provider}; model sessions report {actual}"
                            )
                        # Exercise both sessions: compiled provider availability alone is not enough.
                        detector.detect(
                            np.zeros(
                                (self.detection_size, self.detection_size, 3),
                                dtype=np.uint8,
                            )
                        )
                        raw_probe = np.asarray(recognizer.get_feat(
                            np.zeros((*recognizer.input_size[::-1], 3), dtype=np.uint8))).reshape(-1)
                        if raw_probe.size != DIMENSION:
                            raise ValueError(f"Recognition model produces {raw_probe.size} dimensions; "
                                             f"existing store expects {DIMENSION}. Migration required.")
                        probe = normalize(raw_probe)
                        actual = [
                            model.session.get_providers()[0]
                            for model in (detector, recognizer)
                        ]
                        if actual != [provider, provider]:
                            raise RuntimeError(
                                f"Inference fell back from {provider} to {actual}"
                            )
                        self._record_contract(files, detector, recognizer, len(probe))
                        self._detector, self._recognizer = detector, recognizer
                        with self._status_lock:
                            self._provider, self._state = provider, "ready"
                        label = _provider_label(provider)
                        logger.info(
                            "Face Embedding Device: %s | Face Detection Device: %s | provider=%s",
                            label,
                            label,
                            provider,
                        )
                        break
                    except Exception as exc:
                        errors.append(f"{provider}: {exc}")
                else:
                    raise RuntimeError(
                        "Cannot initialize local models. " + "; ".join(errors)
                    )
            except Exception as exc:
                with self._status_lock:
                    self._state, self._error, self._provider = "error", str(exc), None
                raise RuntimeError(str(exc)) from exc
            return self.status()

    def _record_contract(self, files, detector, recognizer, dimension):
        """Refuse incompatible model replacement; never rewrite legacy vectors."""
        def digest(path):
            with path.open("rb") as source:
                return hashlib.file_digest(source, "sha256").hexdigest()
        embedding = {
            "model": "buffalo_l/w600k_r50.onnx", "sha256": digest(files[1]),
            "dimension": dimension, "alignment": "insightface-five-point-norm_crop",
            "input_size": list(recognizer.input_size), "input_mean": float(recognizer.input_mean),
            "input_std": float(recognizer.input_std), "channel_order": "RGB",
            "normalization": "L2", "similarity": "cosine-inner-product", "format_version": 1,
        }
        if dimension != DIMENSION:
            raise ValueError(f"Model produces {dimension} dimensions; existing face store expects {DIMENSION}. Migration required.")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "face-model.json"
        if path.exists():
            previous = json.loads(path.read_text())
            if previous.get("embedding") != embedding:
                raise ValueError("Face recognition model or preprocessing changed. Use a separate data directory for validation; "
                                 "a side-by-side migration is required before replacing existing embeddings.")
        else:
            legacy = self.data_dir / "embeddings.bin"
            payload = {"embedding": embedding, "legacy_provenance": "unverified" if legacy.exists() and legacy.stat().st_size else "new-store"}
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w") as dest:
                json.dump(payload, dest, indent=2)
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(tmp, path)
        self._metadata = {
            "embedding": embedding,
            "detector": {"model": "buffalo_l/det_10g.onnx", "implementation": type(detector).__name__,
                         "sha256": digest(files[0])},
            "index_version": 2,
        }

    @staticmethod
    def _valid_landmarks(kps, bbox, width, height):
        points = np.asarray(kps, dtype=np.float32)
        if points.shape != (5, 2) or not np.isfinite(points).all():
            return False
        x, y, w, h = bbox
        # Allow moderate profile/edge landmarks, reject degenerate/background crops.
        if (np.any(points[:, 0] < max(-1, x - .25 * w))
                or np.any(points[:, 0] > min(width + 1, x + 1.25 * w))
                or np.any(points[:, 1] < max(-1, y - .25 * h))
                or np.any(points[:, 1] > min(height + 1, y + 1.25 * h))):
            return False
        if not (np.linalg.norm(points[0] - points[1]) >= 1
                and np.linalg.matrix_rank(points - points.mean(axis=0), tol=.1) == 2):
            return False
        return Engine._plausible_face_geometry(points, bbox)

    @staticmethod
    def _plausible_face_geometry(points, bbox):
        """Reject detections whose 5 landmarks cannot belong to a human face.

        Cheap anatomical checks (eyes above nose above mouth along the eye->mouth axis,
        sane box aspect ratio, sane eye spacing). Profile / tilted faces still pass because
        everything is measured along the face's own axis, not the image's vertical.
        """
        _, _, w, h = bbox
        if w <= 0 or h <= 0 or not .55 <= h / w <= 1.9:
            return False
        eye_mid = (points[0] + points[1]) / 2
        mouth_mid = (points[3] + points[4]) / 2
        axis = mouth_mid - eye_mid
        length = float(np.linalg.norm(axis))
        side = float(max(w, h))
        if length < .15 * side:          # eyes and mouth collapsed together
            return False
        t = float(np.dot(points[2] - eye_mid, axis) / (length * length))
        if not .05 <= t <= 1.0:          # nose must sit between eyes and mouth
            return False
        eye_distance = float(np.linalg.norm(points[1] - points[0]))
        if eye_distance < .04 * side or eye_distance > 2.2 * length:
            return False
        return True

    def _detect_at_size(self, image, size):
        """Run detector at a specific input size; restore previous size afterwards."""
        prev = tuple(self._detector.input_size)
        try:
            self._detector.input_size = (int(size), int(size))
            boxes, landmarks = self._detector.detect(image, max_num=0, metric="default")
        finally:
            self._detector.input_size = prev
        return boxes, landmarks

    def detect(self, bgr, multi_scale=None):
        with self._lock:
            try:
                return self._detect_once(bgr, multi_scale)
            except Exception as exc:
                # CoreML can pass a blank startup probe yet fail on real dynamic
                # detector outputs. Retry this same frame using identical weights.
                if self._provider != "CoreMLExecutionProvider" or "CoreML" not in str(exc):
                    raise
                import logging
                logging.getLogger(__name__).warning("CoreML face inference failed; switching both sessions to CPU and retrying frame")
                for model in (self._detector, self._recognizer):
                    model.session.set_providers(["CPUExecutionProvider"])
                with self._status_lock:
                    self._provider = "CPUExecutionProvider"
                return self._detect_once(bgr, multi_scale)

    def _detect_once(self, bgr, multi_scale=None):
        image = np.asarray(bgr)
        if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8 or not image.size:
            raise ValueError("detect expects a nonempty uint8 BGR image")
        image = np.ascontiguousarray(image)
        use_multi = self.multi_scale if multi_scale is None else bool(multi_scale)
        with self._lock:
            self.load()
            from insightface.app.common import Face

            height, width = image.shape[:2]
            all_boxes = []
            all_landmarks = []
            sizes = MULTI_SCALE_SIZES if use_multi else (self.detection_size,)
            for size in sizes:
                boxes, landmarks = self._detect_at_size(image, size)
                if boxes is None or len(boxes) == 0:
                    continue
                for index, box in enumerate(boxes):
                    if not np.isfinite(box).all():
                        continue
                    if (
                        landmarks is None
                        or index >= len(landmarks)
                        or not np.isfinite(landmarks[index]).all()
                    ):
                        continue
                    all_boxes.append(box)
                    all_landmarks.append(landmarks[index])

            candidates = []
            for index, box in enumerate(all_boxes):
                x1, y1, x2, y2 = box[:4]
                x1, y1 = max(0, float(x1)), max(0, float(y1))
                x2, y2 = min(width, float(x2)), min(height, float(y2))
                if x2 - x1 < self.min_face_size or y2 - y1 < self.min_face_size:
                    continue
                bbox = [x1, y1, x2 - x1, y2 - y1]
                det = float(box[4])
                kps = all_landmarks[index]
                if not self.detection_threshold <= det <= 1 or not self._valid_landmarks(kps, bbox, width, height):
                    continue
                quality = face_quality(det, kps=kps, bgr=image, bbox=bbox)
                candidates.append(
                    {
                        "bbox": bbox,
                        "detection": det,
                        "quality": quality,
                        "_kps": kps,
                        "_box4": box[:4],
                    }
                )
            result = deduplicate(candidates)
            for detection in result:
                kps = detection.pop("_kps")
                box4 = detection.pop("_box4")
                # 5-point landmarks (eyes, nose, mouth corners) for eyes-open / smile scoring.
                detection["landmarks"] = [[round(float(x), 2), round(float(y), 2)] for x, y in np.asarray(kps)[:5]]
                face = Face(bbox=box4, kps=kps, det_score=detection["detection"])
                try:
                    self._recognizer.get(image, face)
                    detection["embedding"] = normalize(face.embedding)
                except (ValueError, np.linalg.LinAlgError):
                    # A bad detection must not discard usable faces in the same photo.
                    logger.warning("Skipped unusable aligned face crop", exc_info=True)
            result = Detections((f for f in result if "embedding" in f), result.duplicate_count)
            actual = [
                model.session.get_providers()[0]
                for model in (self._detector, self._recognizer)
            ]
            if actual != [self._provider, self._provider]:
                with self._status_lock:
                    if actual == ["CPUExecutionProvider", "CPUExecutionProvider"]:
                        self._provider = "CPUExecutionProvider"
                    else:
                        self._state, self._error = (
                            "error",
                            f"Model session providers disagree: {actual}",
                        )
                if self._state == "error":
                    raise RuntimeError(self._error)
            return result
