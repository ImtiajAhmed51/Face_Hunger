"""Local-only InsightFace detection and recognition; never download model files.

Supports single-scale and multi-scale detection. Multi-scale runs the detector
at several input sizes, merges boxes with NMS, and attaches a quality score
(pose + blur + det confidence) used downstream for matching and review.
"""

import os
import threading
from pathlib import Path

import numpy as np

from .embeddings import normalize
from .quality import face_quality


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


class Engine:
    def __init__(self, config):
        self.model_dir = Path(config.model_dir) / "buffalo_l"
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
            return {"state": self._state, "provider": self._provider,
                    "available_providers": available, "model": "buffalo_l",
                    "error": self._error, "multi_scale": self.multi_scale,
                    "detection_size": self.detection_size}

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
                    raise FileNotFoundError("Install buffalo_l locally. Missing: " + ", ".join(missing)
                                            + ". Automatic model downloads are disabled.")
                import onnxruntime as ort
                # InsightFace imports Albumentations; disable its online version check too.
                os.environ["NO_ALBUMENTATIONS_UPDATE"] = "1"
                from insightface.model_zoo import get_model

                available = ort.get_available_providers()
                candidates = [p for p in ("CUDAExecutionProvider", "CoreMLExecutionProvider",
                                          "DmlExecutionProvider", "CPUExecutionProvider") if p in available]
                errors = []
                for provider in candidates:
                    try:
                        options = ort.SessionOptions()
                        if provider == "DmlExecutionProvider":
                            options.enable_mem_pattern = False
                            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                        providers = [provider] + (["CPUExecutionProvider"] if provider != "CPUExecutionProvider" else [])
                        detector, recognizer = [get_model(str(p.resolve()), download=False,
                            providers=providers, sess_options=options) for p in files]
                        if detector is None or recognizer is None:
                            raise RuntimeError("Unrecognized buffalo_l ONNX models")
                        if detector.taskname != "detection" or recognizer.taskname != "recognition":
                            raise RuntimeError("Incorrect detection/recognition model files")
                        ctx = -1 if provider == "CPUExecutionProvider" else 0
                        detector.prepare(ctx_id=ctx, input_size=(self.detection_size,) * 2, det_thresh=0.5)
                        recognizer.prepare(ctx_id=ctx)
                        actual = [model.session.get_providers()[0] for model in (detector, recognizer)]
                        if actual != [provider, provider]:
                            raise RuntimeError(f"Requested {provider}; model sessions report {actual}")
                        # Exercise both sessions: compiled provider availability alone is not enough.
                        detector.detect(np.zeros((self.detection_size, self.detection_size, 3), dtype=np.uint8))
                        normalize(recognizer.get_feat(np.zeros((112, 112, 3), dtype=np.uint8)).reshape(-1))
                        actual = [model.session.get_providers()[0] for model in (detector, recognizer)]
                        if actual != [provider, provider]:
                            raise RuntimeError(f"Inference fell back from {provider} to {actual}")
                        self._detector, self._recognizer = detector, recognizer
                        with self._status_lock:
                            self._provider, self._state = provider, "ready"
                        break
                    except Exception as exc:
                        errors.append(f"{provider}: {exc}")
                else:
                    raise RuntimeError("Cannot initialize local models. " + "; ".join(errors))
            except Exception as exc:
                with self._status_lock:
                    self._state, self._error, self._provider = "error", str(exc), None
                raise RuntimeError(str(exc)) from exc
            return self.status()

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
                    if landmarks is None or index >= len(landmarks) or not np.isfinite(landmarks[index]).all():
                        continue
                    all_boxes.append(box)
                    all_landmarks.append(landmarks[index])

            candidates = []
            for index, box in enumerate(all_boxes):
                x1, y1, x2, y2 = box[:4]
                x1, y1 = max(0, float(x1)), max(0, float(y1))
                x2, y2 = min(width, float(x2)), min(height, float(y2))
                if x2 <= x1 or y2 <= y1:
                    continue
                bbox = [x1, y1, x2 - x1, y2 - y1]
                det = float(box[4])
                kps = all_landmarks[index]
                quality = face_quality(det, kps=kps, bgr=image, bbox=bbox)
                candidates.append({
                    "bbox": bbox,
                    "detection": det,
                    "quality": quality,
                    "_kps": kps,
                    "_box4": box[:4],
                })
            result = deduplicate(candidates)
            for detection in result:
                kps = detection.pop("_kps")
                box4 = detection.pop("_box4")
                face = Face(bbox=box4, kps=kps, det_score=detection["detection"])
                self._recognizer.get(image, face)
                detection["embedding"] = normalize(face.embedding)
            actual = [model.session.get_providers()[0] for model in (self._detector, self._recognizer)]
            if actual != [self._provider, self._provider]:
                with self._status_lock:
                    if actual == ["CPUExecutionProvider", "CPUExecutionProvider"]:
                        self._provider = "CPUExecutionProvider"
                    else:
                        self._state, self._error = "error", f"Model session providers disagree: {actual}"
                if self._state == "error":
                    raise RuntimeError(self._error)
            return result