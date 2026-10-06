"""Query rewriting, album generation and captions, with or without the optional local VLM.

The rule-based parser always produces the structured filters. The VLM, when the user has
enabled it and installed the model, only *adds*: alternative visual phrasings for search,
an album title, and one-line captions. Every path works without it, and ``backend.ml.vlm``
is imported lazily inside ``model()`` so a disabled VLM costs nothing at startup.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

VLM_DIR = "smolvlm2-500m"
VLM_FILES = ("vision_encoder.onnx", "embed_tokens.onnx", "decoder.onnx", "tokenizer.json", "config.json")
STOP = {"a", "an", "the", "of", "and", "with", "in", "on", "at", "is", "are", "to", "this", "that", "photo", "image",
        "picture", "there", "it", "its", "his", "her", "their", "from", "for", "be", "has", "have", "which", "who", "by"}
FILLER = re.compile(r"\b(best|favourite|favorite|nice|good|great|top|moments?|photos?|pictures?|pics?|images?|videos?|of|my|our|"
                    r"the|me|some|all|show|find|from|with)\b", re.I)


class Assistant:
    def __init__(self, services):
        self.s = services
        self._model = None  # LocalVLM once loaded (or a test double)

    # -- model access ---------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.s.db.settings().get("vlm_enabled"))

    def _dir(self) -> Path:
        return Path(self.s.config.model_dir) / VLM_DIR

    @property
    def installed(self) -> bool:
        return all((self._dir() / name).is_file() for name in VLM_FILES)

    def model(self):
        """The VLM, or None when it is disabled or not installed. The only place that imports it."""
        if self._model is not None:
            return self._model
        if not self.enabled or not self.installed:
            return None
        from ..ml.vlm import LocalVLM

        self._model = LocalVLM(self.s.config.model_dir, idle_seconds=float(self.s.config.model_idle_seconds))
        return self._model

    def status(self) -> dict:
        base = {"enabled": self.enabled, "model": VLM_DIR, "license": "Apache-2.0", "installed": self.installed,
                "loaded": False, "ram_bytes": 0, "ram_cap_bytes": 4 * 1024 ** 3, "device": None,
                "disk_bytes": sum(p.stat().st_size for p in self._dir().glob("*") if p.is_file()) if self._dir().is_dir() else 0,
                "idle_seconds": float(self.s.config.model_idle_seconds),
                "install_hint": "python scripts/fetch_models.py --only smolvlm2-500m"}
        if self._model is not None:
            base.update(self._model.status())
            base["enabled"] = self.enabled
        return base

    def unload(self) -> None:
        if self._model is not None:
            self._model.unload()

    def close(self) -> None:
        self.unload()
        self._model = None

    # -- query rewriting ------------------------------------------------------------------
    @staticmethod
    def _clean_phrases(raw: str, limit: int = 3) -> list[str]:
        out: list[str] = []
        for line in raw.splitlines():
            line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"').strip()
            if not line or line.endswith(":"):
                continue
            line = re.sub(r"^(a|an|the)?\s*(photo|picture|image)s?\s+(of|showing)\s+", "", line, flags=re.I)
            line = re.split(r"(?<=[.!?])\s", line)[0].rstrip(".")
            words = line.split()
            if 2 <= len(words) <= 18 and line.lower() not in {o.lower() for o in out}:
                out.append(" ".join(words[:14]))
            if len(out) >= limit:
                break
        return out

    def rewrite(self, query: str, *, use_model: bool = True) -> dict:
        """Structured filters (rules, always) plus visual expansions (VLM, when enabled)."""
        from ..routers.search import parse_query

        parsed = parse_query(query, database=self.s.db)
        text = FILLER.sub(" ", parsed["text"])
        text = re.sub(r"\s+", " ", text).strip() or parsed["text"]
        names = {r["id"]: r["name"] for r in self.s.db.all(
            f"SELECT id, name FROM people WHERE id IN ({','.join('?' * len(parsed['people'])) or 'NULL'})", tuple(parsed["people"]))}
        result = {
            "query": query,
            "filters": {"people": parsed["people"], "people_mode": parsed["mode"], "kind": parsed["kind"],
                        "date_from": parsed["date_from"], "date_to": parsed["date_to"]},
            "people_names": [names.get(p, f"Person {p}") for p in parsed["people"]],
            "text": text, "expansions": [], "source": "rules", "model": None,
        }
        model = self.model() if use_model else None
        if model is not None and text:
            try:
                started = time.perf_counter()
                raw = model.generate(f'What would photos of "{text}" show? Answer with a list of 3 short visual descriptions.',
                                     None, max_new_tokens=90)
                phrases = [p for p in self._clean_phrases(raw) if p.lower() != text.lower()]
                if phrases:
                    result.update(expansions=phrases, source="vlm", model=VLM_DIR,
                                  took_ms=round((time.perf_counter() - started) * 1000))
            except Exception as exc:  # the assistant is optional: never fail a search because of it
                logger.warning("query rewriting fell back to rules: %s", exc)
        return result

    def hybrid_query(self, rewritten: dict, **extra) -> dict:
        f = rewritten["filters"]
        return {"text": rewritten["text"] or None, "expansions": rewritten["expansions"], "people": f["people"],
                "people_mode": f["people_mode"], "kind": f["kind"], "date_from": f["date_from"], "date_to": f["date_to"], **extra}

    # -- captions --------------------------------------------------------------------------
    @staticmethod
    def tags_from(caption: str, limit: int = 8) -> list[str]:
        words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", caption)]
        seen: list[str] = []
        for w in words:
            if w not in STOP and w not in seen:
                seen.append(w)
        return seen[:limit]

    def _image(self, row: dict):
        from PIL import Image

        from .. import imaging

        thumb = Path(self.s.config.data_dir) / "thumbnails" / f"media-{row['id']}.jpg"
        if thumb.is_file():
            with Image.open(thumb) as im:
                return im.convert("RGB")
        path = Path(row["path"])
        if row["kind"] == "photo" and path.is_file():
            return imaging.open_image(path, max_side=640)
        return None

    def caption(self, media_id: int, *, force: bool = False) -> Optional[dict]:
        existing = self.s.db.one("SELECT * FROM media_captions WHERE media_id=?", (media_id,))
        if existing and not force:
            return {"caption": existing["caption"], "tags": json.loads(existing["tags"]), "model": existing["model"]}
        model = self.model()
        row = self.s.db.one("SELECT id, path, kind FROM media WHERE id=?", (media_id,))
        if model is None or row is None:
            return None
        image = self._image(row)
        if image is None:
            return None
        text = model.generate("Describe this photo in one short sentence.", image, max_new_tokens=48)
        text = re.split(r"(?<=[.!?])\s", text.strip())[0].strip()
        if len(text) < 4:
            return None
        tags = self.tags_from(text)
        with self.s.db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO media_captions(media_id, caption, tags, model) VALUES (?,?,?,?)",
                         (media_id, text, json.dumps(tags), VLM_DIR))
            conn.execute("DELETE FROM caption_fts WHERE rowid=?", (media_id,))
            conn.execute("INSERT INTO caption_fts(rowid, caption, tags) VALUES (?,?,?)", (media_id, text, " ".join(tags)))
        return {"caption": text, "tags": tags, "model": VLM_DIR}

    def caption_many(self, media_ids: Optional[list[int]], limit: int, checkpoint: Callable[[], None],
                     progress: Callable[..., None]) -> dict:
        if self.model() is None:
            raise RuntimeError("Enable the local VLM in Settings (and install it) to generate captions")
        if media_ids:
            ids = [int(m) for m in media_ids][:limit]
        else:
            ids = [r["id"] for r in self.s.db.all(
                """SELECT m.id FROM media m LEFT JOIN media_captions c ON c.media_id=m.id
                   WHERE m.deleted_at IS NULL AND m.missing=0 AND c.media_id IS NULL ORDER BY m.id DESC LIMIT ?""", (limit,))]
        done = 0
        for index, media_id in enumerate(ids):
            checkpoint()
            if self.caption(media_id):
                done += 1
            progress(processed=index + 1, total=len(ids), captioned=done)
        return {"processed": len(ids), "total": len(ids), "captioned": done}

    # -- album generation ----------------------------------------------------------------------
    def _diverse(self, candidates: list[dict], size: int) -> list[int]:
        """Greedy maximal-marginal-relevance pick: relevance + best-shot, minus similarity to picks."""
        if not candidates:
            return []
        ids = [c["id"] for c in candidates]
        scores = {r["media_id"]: r["score"] for r in self.s.db.all(
            f"""SELECT media_id, score FROM quality_scores WHERE media_id IN ({','.join('?' * len(ids))})
                AND formula_version=(SELECT MAX(formula_version) FROM quality_scores)""", tuple(ids))}
        n = len(candidates)
        relevance = {c["id"]: 0.6 * (1 - i / n) + 0.4 * scores.get(c["id"], 0.5) for i, c in enumerate(candidates)}
        space = self.s.visual_space()
        vectors = {m: v for m in ids if space is not None and (v := space.vector(m)) is not None}
        day = {c["id"]: (c.get("captured_at") or "")[:10] for c in candidates}
        picked: list[int] = []
        remaining = set(ids)
        while remaining and len(picked) < size:
            def gain(m):
                if not picked:
                    return relevance[m]
                if m in vectors:
                    sims = [float(np.dot(vectors[m], vectors[p])) for p in picked if p in vectors]
                    redundancy = max(sims) if sims else 0.0
                else:  # no visual vectors: spread across days instead
                    redundancy = min(1.0, sum(1 for p in picked if day[p] and day[p] == day[m]) / 4)
                return 0.7 * relevance[m] - 0.3 * redundancy
            best = max(remaining, key=gain)
            picked.append(best)
            remaining.discard(best)
        return picked

    def generate_album(self, prompt: str, *, size: int = 30, captions: int = 8, checkpoint: Callable[[], None] = lambda: None,
                       progress: Callable[..., None] = lambda **_: None) -> dict:
        started = time.perf_counter()
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt required")
        progress(phase="understanding", processed=0, total=4)
        rewritten = self.rewrite(prompt)
        checkpoint()
        progress(phase="searching", processed=1, total=4)
        found = self.s.search.run(self.hybrid_query(rewritten, limit=200, page=1))
        candidates = found["items"]
        if not candidates and (rewritten["filters"]["date_from"] or rewritten["filters"]["people"]):
            # Too strict (e.g. no photo matches the words): keep the filters, drop the text.
            relaxed = {**self.hybrid_query(rewritten, limit=200, page=1), "text": None, "expansions": []}
            candidates = self.s.search.run(relaxed)["items"]
        if not candidates:
            raise ValueError("No photos match this prompt")
        checkpoint()
        progress(phase="choosing", processed=2, total=4)
        picked = self._diverse(candidates, max(1, min(size, 200)))
        model = self.model()
        title = None
        if model is not None:
            try:
                raw = model.generate(f"Write a short album title (max 6 words) for: {prompt}. Reply with the title only.",
                                     None, max_new_tokens=16)
                title = re.sub(r"\s+", " ", raw.splitlines()[0]).strip().strip('"').strip("*").strip()[:60] or None
            except Exception as exc:
                logger.warning("album title fell back: %s", exc)
        title = title or (prompt[:1].upper() + prompt[1:60])
        result = self.s.library.create_album(title, picked)
        captioned = 0
        if model is not None:
            progress(phase="captioning", processed=3, total=4)
            for media_id in picked[:max(0, captions)]:
                checkpoint()
                try:
                    if self.caption(media_id):
                        captioned += 1
                except Exception as exc:
                    logger.warning("caption failed for %s: %s", media_id, exc)
        progress(phase="done", processed=4, total=4)
        return {"album_id": result["album_id"], "audit_id": result["audit_id"], "title": title, "items": len(picked),
                "candidates": len(candidates), "captioned": captioned, "source": "vlm" if model is not None else "rules",
                "filters": rewritten["filters"], "text": rewritten["text"], "expansions": rewritten["expansions"],
                "seconds": round(time.perf_counter() - started, 2), "processed": 4, "total": 4}
