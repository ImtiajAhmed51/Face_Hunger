"""Incremental cosine matching and bounded, correction-aware centroid DBSCAN."""

import json
import threading

import numpy as np

from .embeddings import DIMENSION, normalize

ACTIVE = """f.deleted_at IS NULL AND m.deleted_at IS NULL AND m.missing=0
 AND f.review_state!='rejected'
 AND NOT EXISTS (SELECT 1 FROM exclusions e WHERE e.media_id=f.media_id AND e.person_id=f.person_id)
 AND NOT EXISTS (SELECT 1 FROM rejections r WHERE r.face_id=f.id AND r.person_id=f.person_id)"""


class Clustering:
    def __init__(self, db, store):
        self.db, self.store = db, store
        self._lock = threading.RLock()
        self._generation_lock = threading.Lock()
        self._generation = 0
        self._loaded_generation = -1
        self._refreshed_generation = -1
        self._cache = {}
        self._dirty = set()
        self._index = None
        self._ids = np.empty(0, dtype=np.int64)
        self._matrix = np.empty((0, DIMENSION), dtype=np.float32)
        try:
            import faiss
            self._faiss = faiss
        except ImportError:
            self._faiss = None

    def invalidate(self):
        """Call after committing ANY API correction affecting faces/media/people."""
        with self._generation_lock:
            self._generation += 1

    def _threshold(self, conn, value=None):
        if value is None:
            row = conn.execute("SELECT value FROM settings WHERE key='matching_threshold'").fetchone()
            value = json.loads(row[0]) if row else 0.48
        value = float(value)
        if not 0.3 <= value <= 0.8:
            raise ValueError("matching_threshold must be between 0.3 and 0.8")
        return value

    def refresh(self, conn, person_ids=None):
        """Recompute quality-weighted centroids, variance, counts, representatives."""
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        with self._lock:
            generation = self._generation
            ids = sorted(set(int(p) for p in person_ids if p is not None)) if person_ids is not None else None
            last = 0
            position = 0
            while True:
                if ids is None:
                    chunk = [r[0] for r in conn.execute("SELECT id FROM people WHERE id>? ORDER BY id LIMIT 128", (last,))]
                else:
                    chunk = ids[position:position + 128]
                    position += 128
                if not chunk:
                    break
                placeholders = ",".join("?" for _ in chunk)
                sums, weight_sums, counts, representatives, vectors = {}, {}, {}, {}, {}
                cursor = conn.execute(
                    "SELECT f.*, m.kind AS media_kind FROM faces f JOIN media m ON m.id=f.media_id "
                    f"WHERE f.person_id IN ({placeholders}) AND {ACTIVE} ORDER BY f.id", chunk)
                while rows := cursor.fetchmany(256):
                    for face in rows:
                        pid = face["person_id"]
                        vector = self.store.read(face["embedding_offset"], face["embedding_sha"])
                        q = float(face["quality"] if face["quality"] is not None else 0.5)
                        # Confirmed / manual faces get a modest quality boost so user
                        # corrections anchor the centroid more strongly.
                        if face["review_state"] == "confirmed" or face["manual"]:
                            q = min(1.0, q + 0.15)
                        weight = max(0.15, q)
                        if pid not in sums:
                            sums[pid] = np.zeros(DIMENSION, dtype=np.float64)
                            weight_sums[pid] = 0.0
                            counts[pid] = 0
                            vectors[pid] = []
                        sums[pid] += vector * weight
                        weight_sums[pid] += weight
                        counts[pid] += 1
                        vectors[pid].append(vector)
                        # Prefer still photos over video frames for the person tile.
                        is_photo = 1 if face["media_kind"] == "photo" else 0
                        score = (
                            is_photo,
                            face["review_state"] == "confirmed",
                            q,
                            face["detection"],
                            -face["id"],
                        )
                        if pid not in representatives or score > representatives[pid][0]:
                            representatives[pid] = (score, face["id"])
                for pid in chunk:
                    count = counts.get(pid, 0)
                    wsum = weight_sums.get(pid, 0.0)
                    if count and wsum > 1e-12 and np.linalg.norm(sums[pid]) > 1e-12:
                        centroid = normalize(sums[pid] / wsum)
                        # Mean cosine distance to centroid ≈ intra-person variance proxy
                        sims = [float(centroid @ v) for v in vectors[pid]]
                        variance = float(np.mean([max(0.0, 1.0 - s) for s in sims])) if sims else 0.0
                    else:
                        centroid, variance = None, 0.0
                    conn.execute(
                        "UPDATE people SET centroid=?,face_count=?,representative_face_id=?,variance=? WHERE id=?",
                        (centroid.astype("<f4").tobytes() if centroid is not None else None,
                         count, representatives[pid][1] if count else None, variance, pid))
                    if ids is not None and self._loaded_generation == generation:
                        if centroid is None:
                            self._cache.pop(pid, None)
                        else:
                            self._cache[pid] = (centroid, count, variance)
                        self._dirty.add(pid)
                last = chunk[-1]
            if ids is None:
                self._refreshed_generation = generation
                self._loaded_generation = -1

    def _load_cache(self, conn):
        generation = self._generation
        if self._loaded_generation == generation:
            return
        if self._refreshed_generation != generation:
            self.refresh(conn)
        self._cache = {}
        for row in conn.execute(
            "SELECT id,centroid,face_count,variance FROM people WHERE face_count>0 AND centroid IS NOT NULL"
        ):
            vector = normalize(np.frombuffer(row["centroid"], dtype="<f4"))
            variance = float(row["variance"] if row["variance"] is not None else 0.0)
            self._cache[row["id"]] = (vector, row["face_count"], variance)
        self._rebuild()
        self._loaded_generation = generation

    def _rebuild(self):
        self._ids = np.asarray(list(self._cache), dtype=np.int64)
        self._matrix = (np.stack([self._cache[int(p)][0] for p in self._ids]).astype(np.float32)
                        if len(self._ids) else np.empty((0, DIMENSION), dtype=np.float32))
        self._index = None
        if self._faiss is not None:
            self._index = self._faiss.IndexFlatIP(DIMENSION)
            if len(self._matrix):
                self._index.add(self._matrix)
        self._dirty.clear()

    def _candidates(self, vector, threshold):
        if len(self._dirty) >= 128:
            self._rebuild()
        result = []
        if len(self._ids):
            if self._index is not None:
                _, scores, indices = self._index.range_search(vector[None, :], threshold - 1e-7)
                pairs = zip(indices, scores)
            else:
                scores = self._matrix @ vector
                indices = np.flatnonzero(scores >= threshold)
                pairs = ((i, scores[i]) for i in indices)
            result.extend((int(self._ids[i]), float(score)) for i, score in pairs
                          if int(self._ids[i]) not in self._dirty)
        result.extend((pid, float(self._cache[pid][0] @ vector)) for pid in self._dirty
                      if pid in self._cache and float(self._cache[pid][0] @ vector) >= threshold)
        return sorted(result, key=lambda item: (-item[1], item[0]))

    def assign(self, conn, embedding, media_id=None, timestamp=None, face_id=None, threshold=None):
        """Return (person_id, cosine_similarity); caller assigns the face in this transaction.

        Pass media_id and timestamp (None for a photo) to enforce co-occurrence.
        Pass face_id when reassigning so persisted face/person rejections apply.
        """
        vector = normalize(embedding)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        with self._lock:
            threshold = self._threshold(conn, threshold)
            face = None
            if face_id is not None:
                face = conn.execute("SELECT * FROM faces WHERE id=?", (face_id,)).fetchone()
                if face is None:
                    raise ValueError("Face does not exist")
                if media_id is not None and media_id != face["media_id"]:
                    raise ValueError("Face belongs to a different media item")
                media_id, timestamp = face["media_id"], face["timestamp"]
                if face["deleted_at"] is not None or face["review_state"] == "rejected":
                    return None, None
            if media_id is not None:
                media = conn.execute("SELECT deleted_at,missing,kind FROM media WHERE id=?", (media_id,)).fetchone()
                if media is None or media["deleted_at"] is not None or media["missing"]:
                    raise ValueError("Cannot cluster faces from inactive media")
                if media["kind"] == "photo":
                    timestamp = None
            self._load_cache(conn)
            blocked = set()
            if media_id is not None:
                blocked.update(r[0] for r in conn.execute("SELECT person_id FROM exclusions WHERE media_id=?", (media_id,)))
                blocked.update(r[0] for r in conn.execute("SELECT person_id FROM faces WHERE media_id=? "
                    "AND deleted_at IS NULL AND person_id IS NOT NULL AND id!=? "
                    "AND (timestamp IS NULL OR ? IS NULL OR ABS(timestamp-?)<0.001)",
                    (media_id, face_id or -1, timestamp, timestamp)))
            if face_id is not None:
                blocked.update(r[0] for r in conn.execute("SELECT person_id FROM rejections WHERE face_id=?", (face_id,)))
                current = face["person_id"] if face is not None else None
                if current is not None:
                    blocked.update(r[0] for r in conn.execute("SELECT CASE WHEN person_a=? THEN person_b ELSE person_a END "
                        "FROM separate_people WHERE person_a=? OR person_b=?", (current, current, current)))
                    named = conn.execute("SELECT name FROM people WHERE id=?", (current,)).fetchone()
                    forbidden = conn.execute("SELECT 1 FROM exclusions WHERE person_id=? AND media_id=? "
                        "UNION ALL SELECT 1 FROM rejections WHERE person_id=? AND face_id=?",
                        (current, media_id, current, face_id)).fetchone()
                    if forbidden:
                        return current, None
                    elif face["manual"] or face["review_state"] == "confirmed" or (named and named[0]):
                        return current, float(vector @ self._cache[current][0]) if current in self._cache else None
            # Hard negatives: previously rejected (face, person) pairs lower effective score
            hard = {}
            if face_id is not None:
                for r in conn.execute(
                    "SELECT person_id, similarity FROM hard_negatives WHERE face_id=?", (face_id,)
                ):
                    hard[int(r["person_id"])] = float(r["similarity"] or 0)
            for pid, similarity in self._candidates(vector, threshold - 0.05):
                if pid in blocked:
                    continue
                entry = self._cache[pid]
                centroid, count = entry[0], entry[1]
                variance = entry[2] if len(entry) > 2 else 0.0
                # Adaptive threshold: tight clusters (low variance) need higher similarity;
                # sparse person clusters allow a slightly looser match.
                adaptive = threshold + max(-0.04, min(0.04, 0.06 - variance * 0.5))
                if hard.get(pid) is not None:
                    # Previously rejected against this person → require stronger evidence
                    adaptive = min(0.8, adaptive + 0.06)
                if similarity < adaptive:
                    continue
                # Running approximation replaced by exact refresh per media.
                self._cache[pid] = (normalize(centroid * count + vector), count + 1, variance)
                self._dirty.add(pid)
                return pid, min(1.0, similarity)
            pid = conn.execute(
                "INSERT INTO people(centroid,face_count,variance) VALUES (?,0,0)",
                (vector.astype("<f4").tobytes(),),
            ).lastrowid
            self._cache[pid] = (vector, 1, 0.0)
            self._dirty.add(pid)
            # A new identity has no independent match score, not "100% certain".
            return pid, None

    def _protected(self, conn):
        return {r[0] for r in conn.execute("""SELECT id FROM people WHERE TRIM(COALESCE(name,''))!=''
          UNION SELECT person_id FROM faces WHERE person_id IS NOT NULL AND (manual=1 OR review_state!='unreviewed')
          UNION SELECT person_id FROM exclusions UNION SELECT person_id FROM rejections
          UNION SELECT person_a FROM separate_people UNION SELECT person_b FROM separate_people""")}

    def _collision(self, conn, a, b):
        return conn.execute("""SELECT 1 FROM faces f JOIN media m ON m.id=f.media_id
          JOIN faces g ON g.media_id=f.media_id AND g.person_id=? AND g.deleted_at IS NULL
          WHERE f.person_id=? AND """ + ACTIVE + """ AND g.review_state!='rejected'
          AND (f.timestamp IS NULL OR g.timestamp IS NULL OR ABS(f.timestamp-g.timestamp)<0.001) LIMIT 1""",
          (b, a)).fetchone() is not None

    def reconcile(self, callback=None, checkpoint=None, max_neighbors=64, max_centroids=20000):
        """DBSCAN(min_samples=2) on a bounded cosine-neighbor centroid graph.

        Named/corrected/constrained people are never auto-merged. Large libraries
        fail explicitly at the centroid limit, never silently reconcile a subset.
        callback receives a progress dict; checkpoint may raise to cancel.
        """
        from scipy.sparse import coo_matrix
        from sklearn.cluster import DBSCAN

        if not 2 <= max_neighbors <= 256 or not 1 <= max_centroids <= 100000:
            raise ValueError("Invalid reconciliation bounds")

        def check():
            if checkpoint:
                checkpoint()

        def report(**values):
            progress.update(values)
            if callback:
                callback(dict(progress))

        progress = {"phase": "reconciling", "processed": 0, "total": 0, "merged": 0}
        check()
        with self.db.connect() as conn:
            threshold = self._threshold(conn)
            self.refresh(conn)
            protected = self._protected(conn)
            rows = []
            for row in conn.execute("SELECT id,centroid FROM people WHERE face_count>0 AND centroid IS NOT NULL ORDER BY id"):
                if row["id"] not in protected:
                    rows.append(dict(row))
                    if len(rows) > max_centroids:
                        break
        if len(rows) > max_centroids:
            raise ValueError(f"Reconciliation exceeds the safety limit of {max_centroids} eligible centroids")
        report(total=len(rows))
        if len(rows) < 2:
            report(processed=len(rows))
            return progress
        ids = [row["id"] for row in rows]
        matrix = np.stack([normalize(np.frombuffer(row["centroid"], dtype="<f4")) for row in rows])
        k = min(max_neighbors + 1, len(ids))
        index = None
        if self._faiss is not None:
            index = self._faiss.IndexHNSWFlat(DIMENSION, 32, self._faiss.METRIC_INNER_PRODUCT)
            index.hnsw.efConstruction = 80
            index.hnsw.efSearch = max(128, k)
            for start in range(0, len(ids), 256):
                check()
                index.add(matrix[start:start + 256])
        rr, cc, dd = [], [], []
        for start in range(0, len(ids), 128):
            check()
            query = matrix[start:start + 128]
            if index is not None:
                scores, neighbors = index.search(query, k)
            else:
                similarities = query @ matrix.T
                neighbors = np.argpartition(-similarities, k - 1, axis=1)[:, :k]
                scores = np.take_along_axis(similarities, neighbors, axis=1)
            for offset, (near, similarities) in enumerate(zip(neighbors, scores)):
                for neighbor, similarity in zip(near, similarities):
                    if neighbor >= 0 and similarity >= threshold:
                        rr.append(start + offset)
                        cc.append(int(neighbor))
                        dd.append(max(1e-7, 1.0 - float(similarity)))
            report(phase="neighbors", processed=min(start + 128, len(ids)))
        graph = coo_matrix((dd, (rr, cc)), shape=(len(ids), len(ids))).tocsr()
        graph = graph.maximum(graph.T)
        graph.setdiag(1e-7)
        check()
        labels = DBSCAN(eps=1.0 - threshold + 1e-6, min_samples=2, metric="precomputed", n_jobs=1).fit_predict(graph)
        groups = {}
        for pid, label in zip(ids, labels):
            if label >= 0:
                groups.setdefault(int(label), []).append(pid)
        report(phase="merging", processed=0)
        processed = 0
        for group in groups.values():
            target = min(group)
            for source in sorted(p for p in group if p != target):
                check()
                with self.db.connect() as conn:
                    # Acquire the write lock before re-reading user corrections.
                    conn.execute("BEGIN IMMEDIATE")
                    protected = self._protected(conn)
                    if target in protected or source in protected:
                        continue
                    pair = {r["id"]: dict(r) for r in conn.execute(
                        "SELECT id,centroid FROM people WHERE id IN (?,?) AND face_count>0", (target, source))}
                    if len(pair) != 2 or any(r["centroid"] is None for r in pair.values()):
                        continue
                    a, b = [normalize(np.frombuffer(pair[p]["centroid"], dtype="<f4")) for p in (target, source)]
                    if float(a @ b) < threshold or self._collision(conn, target, source):
                        continue
                    conn.execute("UPDATE faces SET person_id=? WHERE person_id=? AND id IN "
                        "(SELECT f.id FROM faces f JOIN media m ON m.id=f.media_id WHERE " + ACTIVE + ")",
                        (target, source))
                    self.refresh(conn, [target, source])
                    # Retain empty source IDs: inactive faces may still refer to them.
                    progress["merged"] += 1
                processed += 1
                report(processed=processed)
        self.invalidate()
        report(phase="reconciling", processed=len(ids))
        return progress