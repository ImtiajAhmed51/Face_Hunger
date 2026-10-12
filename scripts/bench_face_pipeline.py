"""Safe face-pipeline measurements on disposable data and optional read-only samples.

Run: .venv/bin/python scripts/bench_face_pipeline.py [--models] [--library-samples 8]
Never modifies production media, face assignments, embeddings or indexes.
"""
import argparse
import json
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.clustering import Clustering  # noqa: E402
from backend.config import Config  # noqa: E402
from backend.db import Database  # noqa: E402
from backend.embeddings import EmbeddingStore  # noqa: E402
from backend.vectors.ann import AnnIndex  # noqa: E402


def emit(**values):
    print(json.dumps(values), flush=True)


def measured_centroid(folder):
    db = Database(folder / 'index.sqlite')
    with EmbeddingStore(folder / 'embeddings.bin') as store:
        v = np.zeros(512, np.float32); v[0] = 1
        offset, sha = store.append(v)
        with db.connect() as c:
            c.execute("INSERT INTO libraries(id,path,name) VALUES (1,'/synthetic','Benchmark')")
            c.execute("INSERT INTO media(id,library_id,path,name,kind,size,mtime_ns,status) "
                      "VALUES (1,1,'/synthetic/image.jpg','image.jpg','photo',1,1,'indexed')")
            pid = c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
            c.executemany("INSERT INTO faces(media_id,person_id,bbox,detection,embedding_offset,embedding_sha) "
                          "VALUES (1,?,'[0,0,20,20]',.9,?,?)", [(pid, offset, sha)] * 10000)
        cluster = Clustering(db, store)
        tracemalloc.start(); started = time.perf_counter()
        with db.connect() as c:
            cluster.refresh(c)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        emit(benchmark='centroid',faces=10000,seconds=elapsed,temporary_python_MiB=peak / 2**20)


def measured_ann(folder):
    import faiss
    faiss.omp_set_num_threads(2)
    rng = np.random.default_rng(12)
    vectors = rng.normal(size=(10000,512)).astype(np.float32)
    vectors /= np.linalg.norm(vectors,axis=1,keepdims=True)
    queries = vectors[:50] + rng.normal(0,.008,size=(50,512)).astype(np.float32)
    queries /= np.linalg.norm(queries,axis=1,keepdims=True)
    exact = faiss.IndexFlatIP(512); exact.add(vectors)
    started=time.perf_counter(); _, truth = exact.search(queries,10); flat_ms=(time.perf_counter()-started)*1000/len(queries)
    ann = AnnIndex(folder/'fixture.usearch',512,dtype='i8')
    started=time.perf_counter(); ann.upsert(range(1,10001),vectors); build=time.perf_counter()-started
    started=time.perf_counter(); got = [ann.search(q,10)[0]-1 for q in queries]; ann_ms=(time.perf_counter()-started)*1000/len(queries)
    recall=np.mean([len(set(a)&set(b))/10 for a,b in zip(got,truth)])
    emit(benchmark='synthetic_ann',dtype='i8',vectors=10000,queries=50,build_seconds=build,flat_query_ms=flat_ms,
         ann_query_ms=ann_ms,recall_at_10=float(recall),top1_agreement=float(np.mean([a[0]==b[0] for a,b in zip(got,truth)])))


def measured_models(folder, library_samples):
    import cv2
    import onnxruntime as ort

    from backend.engine import Engine
    from backend.media_processing import load_image
    from tests.helpers.video_fixture import INSIGHT, _face_b
    # Both providers have their own disposable contract; no model downloads.
    images=[('blank',np.zeros((640,640,3),np.uint8)),('single',cv2.cvtColor(np.array(_face_b()),cv2.COLOR_RGB2BGR))]
    multi=cv2.imread(str(INSIGHT/'t1.jpg'))
    images += [('multiple',multi),('small',cv2.resize(multi,(256,144))),('blur',cv2.GaussianBlur(multi,(31,31),10))]
    if library_samples:
        with sqlite3.connect(f'file:{ROOT / "data/index.sqlite"}?mode=ro',uri=True) as c:
            rows=c.execute("SELECT path FROM media WHERE kind='photo' AND status='indexed' AND missing=0 "
                           "AND deleted_at IS NULL ORDER BY id LIMIT ?",(library_samples,)).fetchall()
        for i,(path,) in enumerate(rows):
            try:
                images.append((f'local_sample_{i+1}',load_image(path,max_side=2048)))
            except Exception as exc:
                emit(sample=f'local_sample_{i+1}',error=type(exc).__name__)
    for preference in ('cpu','auto'):
        engine=Engine(Config(data_dir=folder/preference,model_dir=ROOT/'models',face_provider=preference))
        started=time.perf_counter(); engine.load()
        emit(benchmark='model_load',preference=preference,seconds=time.perf_counter()-started,providers=ort.get_available_providers(),
             provider=engine.status()['provider'],metadata=engine.status()['metadata'])
        for name,image in images:
            durations=[]
            for _ in range(3):
                started=time.perf_counter(); faces=engine.detect(image); durations.append(time.perf_counter()-started)
            emit(benchmark='detect',provider=preference,sample=name,faces=len(faces),median_seconds=float(np.median(durations)))
        del engine


def measured_review_pairs():
    """Read-only proxy labels from explicit assignments; never tune live settings."""
    import hashlib
    import itertools
    import os

    from backend.embeddings import normalize
    database = ROOT / 'data/index.sqlite'
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as c:
        rows = c.execute("SELECT f.person_id,f.media_id,f.embedding_offset,f.embedding_sha FROM faces f "
                         "JOIN media m ON m.id=f.media_id JOIN people p ON p.id=f.person_id "
                         "WHERE f.manual=1 AND f.review_state='confirmed' AND f.deleted_at IS NULL "
                         "AND m.deleted_at IS NULL AND m.missing=0 AND TRIM(COALESCE(p.name,''))!='' "
                         "ORDER BY f.id DESC LIMIT 2000").fetchall()
    groups, seen = {}, {}
    fd = os.open(ROOT / 'data/embeddings.bin', os.O_RDONLY)
    try:
        for pid, mid, offset, sha in rows:
            if len(groups.get(pid, [])) >= 5 or mid in seen.get(pid, set()):
                continue
            raw = os.pread(fd, 2048, offset)
            if len(raw) != 2048 or hashlib.sha256(raw).hexdigest() != sha:
                continue
            groups.setdefault(pid, []).append(normalize(np.frombuffer(raw, dtype='<f4')))
            seen.setdefault(pid, set()).add(mid)
    finally:
        os.close(fd)
    positives = [float(a @ b) for vectors in groups.values() for a, b in itertools.combinations(vectors, 2)]
    negatives = [float(a[0] @ b[0]) for a, b in itertools.combinations(groups.values(), 2)]
    if not positives or not negatives:
        emit(benchmark='review_pair_thresholds',limitation='Insufficient cross-media human identity labels',
             positive_pairs=len(positives),negative_pairs=len(negatives))
        return
    for threshold in (.40,.44,.48,.52,.56,.60,.64,.68):
        emit(benchmark='review_pair_thresholds',threshold=threshold,people=len(groups),positive_pairs=len(positives),
             negative_pairs=len(negatives),false_match_rate=float(np.mean(np.array(negatives)>=threshold)),
             missed_match_rate=float(np.mean(np.array(positives)<threshold)),
             limitation='Uncurated manual labels, correlated pairs, legacy model provenance unverified; not held-out calibration')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review-pairs',action='store_true')
    parser.add_argument('--models-only',action='store_true')
    parser.add_argument('--models',action='store_true')
    parser.add_argument('--library-samples',type=int,default=0,choices=range(0,21))
    args=parser.parse_args()
    if args.review_pairs:
        measured_review_pairs()
    with tempfile.TemporaryDirectory(prefix='face-hunger-benchmark-') as temp:
        folder=Path(temp)
        if args.models_only:
            measured_models(folder,args.library_samples)
            return
        measured_centroid(folder)
        measured_ann(folder)
        if args.models or args.library_samples:
            # Keep incompatible optional OpenMP runtimes out of one process.
            import subprocess
            subprocess.run([sys.executable, str(Path(__file__).resolve()), '--models-only',
                            '--library-samples', str(args.library_samples)], check=True, cwd=ROOT)


if __name__ == '__main__':
    main()
