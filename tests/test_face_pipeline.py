"""Face pipeline integrity tests; all databases and vectors are disposable."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from backend.clustering import Clustering
from backend.db import Database
from backend.embeddings import EmbeddingStore, normalize
from backend.engine import Engine
from backend.threshold_tuning import _sweep, autotune
from backend.vectors.ann import AnnIndex
from backend.vectors.spaces import VectorSpaces
from backend.vectors.specs import FACE_ARCFACE
from tests.helpers.fixtures import add_media
from tests.test_redesign import seed_faces


def vector(i=0):
    v = np.zeros(512, np.float32)
    v[i] = 1
    return v


def insert_face(db, store, v, person=None, media=1):
    offset, sha = store.append(v)
    with db.connect() as c:
        return c.execute("INSERT INTO faces(media_id,person_id,bbox,detection,embedding_offset,embedding_sha) "
                         "VALUES (?,?,'[0,0,20,20]',.9,?,?)", (media, person, offset, sha)).lastrowid


@pytest.mark.parametrize('bad', [np.zeros(512), np.ones(511), np.full(512, np.nan), np.full(512, np.inf)])
def test_embedding_rejects_invalid_input(bad):
    with pytest.raises(ValueError):
        normalize(bad)


def test_embedding_batch_is_atomic_and_unit_length(tmp_path, monkeypatch):
    store = EmbeddingStore(tmp_path / 'embeddings.bin')
    try:
        with pytest.raises(ValueError):
            store.append_many([vector(), np.zeros(512)])
        assert store.path.stat().st_size == 0
        import backend.embeddings as module
        original = module.os.fsync
        calls = []
        monkeypatch.setattr(module.os, 'fsync', lambda fd: (calls.append(fd), original(fd))[1])
        records = store.append_many([vector(), vector(1) * 7, vector(2)])
        assert len(calls) == 1
        assert [o for o, _ in records] == [0, 2048, 4096]
        assert float(store.read(*records[0]) @ store.read(*records[1])) == 0
        assert float(store.read(*records[1]) @ store.read(*records[1])) == pytest.approx(1)
    finally:
        store.close()


def test_face_index_incremental_replacement_deletion_and_restart(tmp_path):
    db = Database(tmp_path / 'index.sqlite')
    add_media(db, 1)
    with EmbeddingStore(tmp_path / 'embeddings.bin') as store:
        fid = insert_face(db, store, vector())
        spaces = VectorSpaces(db, tmp_path)
        space = spaces.get(FACE_ARCFACE.key)
        assert space.sync()['action'] == 'rebuild'
        o, h = store.append(vector(1))
        with db.connect() as c:
            c.execute('UPDATE faces SET embedding_offset=?,embedding_sha=? WHERE id=?', (o, h, fid))
        space.ensure_ready()
        assert space.ann.search(vector(1), 1)[1][0] > .999
        second = insert_face(db, store, vector(2))
        space.ensure_ready()
        assert space.ann.search(vector(2), 1)[0][0] == second
        spaces.close()
        spaces = VectorSpaces(db, tmp_path)
        again = spaces.get(FACE_ARCFACE.key)
        assert again.sync()['action'] == 'catch_up'
        assert again.ann.search(vector(1), 1)[0][0] == fid
        with db.connect() as c:
            c.execute('DELETE FROM faces WHERE id=?', (fid,))
        again.ensure_ready()
        assert again.ann.keys().tolist() == [second]
        spaces.close()


def test_change_journal_rolls_back_with_database(tmp_path):
    db = Database(tmp_path / 'index.sqlite')
    add_media(db, 1)
    with EmbeddingStore(tmp_path / 'embeddings.bin') as store:
        fid = insert_face(db, store, vector())
        before = db.one('SELECT MAX(seq) s FROM vector_changes')['s']
        with pytest.raises(RuntimeError), db.connect() as c:
            c.execute('DELETE FROM faces WHERE id=?', (fid,))
            raise RuntimeError('interrupted')
        assert db.one('SELECT MAX(seq) s FROM vector_changes')['s'] == before
        assert db.one('SELECT id FROM faces WHERE id=?', (fid,))


def test_index_reconciles_equal_count_wrong_ids(tmp_path):
    db = Database(tmp_path / 'index.sqlite')
    add_media(db, 1)
    with EmbeddingStore(tmp_path / 'embeddings.bin') as store:
        fid = insert_face(db, store, vector())
        spaces = VectorSpaces(db, tmp_path)
        s = spaces.get(FACE_ARCFACE.key)
        s.sync()
        s.ann.remove([fid])
        s.ann.upsert([999], vector(1)[None])
        s.maybe_save(force=True)
        spaces.close()
        spaces = VectorSpaces(db, tmp_path)
        s = spaces.get(FACE_ARCFACE.key)
        s.sync()
        assert s.ann.keys().tolist() == [fid]
        assert s.ann.search(vector(), 1)[1][0] > .999
        spaces.close()


def test_index_detects_corruption_and_crash_between_index_and_metadata(tmp_path):
    index = AnnIndex(tmp_path / 'test.usearch', 512)
    index.upsert([1], vector()[None])
    index.save(key='test')
    assert AnnIndex(index.path, 512).load()
    index.upsert([1], vector(1)[None])
    index.index.save(str(index.path))  # crash before matching metadata publication
    assert not AnnIndex(index.path, 512).load()
    index.path.write_bytes(b'corrupt')
    assert not AnnIndex(index.path, 512).load()


def test_ann_rejects_zero_nan_duplicate_and_dimension_mismatch(tmp_path):
    index = AnnIndex(tmp_path / 'test.usearch', 512)
    for ids, values in [([1], np.zeros((1, 512))), ([1], np.full((1, 512), np.nan)),
                        ([1, 1], np.ones((2, 512))), ([1], np.ones((1, 511)))]:
        with pytest.raises(ValueError):
            index.upsert(ids, values)
    assert len(index) == 0


def test_manual_confirmation_of_automatic_face_creates_calibration_label(client, app_services):
    seed_faces(app_services, 1)
    with app_services.db.connect() as c:
        c.execute("UPDATE faces SET review_state='confirmed',manual=0,similarity=.7")
    r = client.post('/api/faces/review', json={'face_ids': [1], 'decision': 'yes'})
    assert r.status_code == 200
    assert app_services.db.one('SELECT manual FROM faces')['manual'] == 1
    assert app_services.db.one('SELECT correct FROM face_match_feedback')['correct'] == 1
    assert client.post(f"/api/audit/{r.json()['audit_id']}/undo").status_code == 200
    assert app_services.db.one('SELECT COUNT(*) c FROM face_match_feedback')['c'] == 0


def test_calibration_excludes_automatic_and_legacy_manual_scores(client, app_services):
    seed_faces(app_services, 60)
    with app_services.db.connect() as c:
        c.execute("UPDATE faces SET review_state='confirmed',similarity=.6")
    assert autotune(app_services.db) is None
    with app_services.db.connect() as c:
        c.execute('UPDATE faces SET manual=1')
    assert autotune(app_services.db) is None
    # New explicit review history with both classes is required.
    with app_services.db.connect() as c:
        c.execute("UPDATE faces SET manual=0,review_state='unreviewed',similarity=CASE WHEN id<=30 THEN .7 ELSE .4 END")
    client.post('/api/faces/review', json={'face_ids': list(range(1, 31)), 'decision': 'yes'})
    assert autotune(app_services.db) is None  # positives alone cannot calibrate
    client.post('/api/faces/review', json={'face_ids': list(range(31, 61)), 'decision': 'no'})
    result = autotune(app_services.db)
    assert result['n_samples'] == 60 and result['precision'] == 1 and not result['validated']
    assert .4 < result['suggested'] <= .7


def test_threshold_sweep_does_not_round_away_positive_boundary():
    threshold, _, precision, recall = _sweep([(.60000049, True), (.6000004, False)])
    assert threshold == .60000049 and precision == recall == 1


def test_model_contract_prevents_incompatible_replacement_without_touching_vectors(tmp_path):
    config = SimpleNamespace(model_dir=tmp_path / 'models', data_dir=tmp_path)
    engine = Engine(config)
    detector = SimpleNamespace()
    recognizer = SimpleNamespace(input_size=(112, 112), input_mean=127.5, input_std=127.5)
    det, rec = tmp_path / 'det.onnx', tmp_path / 'rec.onnx'
    det.write_bytes(b'detector'); rec.write_bytes(b'recognizer')
    existing = tmp_path / 'embeddings.bin'; existing.write_bytes(b'legacy')
    engine._record_contract([det, rec], detector, recognizer, 512)
    assert json.loads((tmp_path / 'face-model.json').read_text())['legacy_provenance'] == 'unverified'
    rec.write_bytes(b'different model')
    with pytest.raises(ValueError, match='side-by-side migration'):
        engine._record_contract([det, rec], detector, recognizer, 512)
    assert existing.read_bytes() == b'legacy'


def test_alignment_rejects_degenerate_and_background_landmarks():
    bbox = [10, 10, 50, 60]
    valid = np.array([[25, 30], [45, 30], [35, 40], [27, 55], [43, 55]])
    assert Engine._valid_landmarks(valid, bbox, 100, 100)
    for bad in (np.zeros((5, 2)), valid + 500, np.ones((4, 2)), np.full((5, 2), np.nan)):
        assert not Engine._valid_landmarks(bad, bbox, 100, 100)


def test_streaming_centroid_variance_matches_direct_cosine(tmp_path):
    db = Database(tmp_path / 'index.sqlite'); add_media(db, 1)
    with db.connect() as c:
        pid = c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
    vectors = [normalize(np.random.default_rng(i).normal(size=512)) for i in range(12)]
    with EmbeddingStore(tmp_path / 'embeddings.bin') as store:
        for v in vectors:
            insert_face(db, store, v, pid)
        with db.connect() as c:
            Clustering(db, store).refresh(c)
        row = db.one('SELECT centroid,variance,face_count FROM people WHERE id=?', (pid,))
        centroid = np.frombuffer(row['centroid'], dtype='<f4')
        assert row['variance'] == pytest.approx(np.mean([max(0, 1 - float(centroid @ v)) for v in vectors]), abs=1e-7)
        assert row['face_count'] == 12


def test_evaluation_excludes_query_from_own_centroid(tmp_path):
    from backend.evaluation import evaluate
    db = Database(tmp_path / 'index.sqlite'); add_media(db, 1)
    with EmbeddingStore(tmp_path / 'embeddings.bin') as store:
        for i in range(5):
            with db.connect() as c:
                pid = c.execute('INSERT INTO people(name) VALUES (?)', (f'Human {i}',)).lastrowid
            for j in range(4):
                fid = insert_face(db, store, vector(i * 4 + j), pid)
                with db.connect() as c:
                    c.execute("UPDATE faces SET manual=1,review_state='confirmed' WHERE id=?", (fid,))
        result = evaluate(db, store, .3)
        assert result['n_faces'] == 20 and result['mean_correct_sim'] == 0
        assert result['tp'] == result['fp'] == 0 and result['fn'] == 20
        assert result['missed_match_rate'] == 1


def test_reconcile_isolated_singletons_are_unassigned_and_undoable(client, app_services):
    s = app_services
    ids = add_media(s.db, 2)
    pids = []
    for i, mid in enumerate(ids):
        with s.db.connect() as c:
            pid = c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
        pids.append(pid)
        insert_face(s.db, s.store, vector(i), pid, mid)
    result = s.cluster.reconcile()
    assert result['merged'] == 0 and result['noise_unassigned'] == 2
    assert all(r['person_id'] is None for r in s.db.all('SELECT person_id FROM faces'))
    assert client.get('/api/review').json()['total'] == 2
    assert client.post(f"/api/audit/{result['noise_audit_id']}/undo").status_code == 200
    assert [r['person_id'] for r in s.db.all('SELECT person_id FROM faces ORDER BY id')] == pids


def test_reconcile_preserves_manual_and_existing_multiface_identity(client, app_services):
    s = app_services
    ids = add_media(s.db, 3)
    with s.db.connect() as c:
        multi = c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
        manual = c.execute("INSERT INTO people(name) VALUES ('Human')").lastrowid
    insert_face(s.db, s.store, vector(), multi, ids[0])
    insert_face(s.db, s.store, vector(), multi, ids[1])
    fid = insert_face(s.db, s.store, vector(1), manual, ids[2])
    with s.db.connect() as c:
        c.execute("UPDATE faces SET manual=1,review_state='confirmed' WHERE id=?", (fid,))
    assert s.cluster.reconcile()['noise_unassigned'] == 0
    assert s.db.one('SELECT person_id FROM faces WHERE id=?', (fid,))['person_id'] == manual
    assert s.db.one('SELECT COUNT(*) c FROM faces WHERE person_id=?', (multi,))['c'] == 2


def test_conflicting_mac_openmp_wheels_use_safe_fallback(tmp_path, monkeypatch):
    from backend import faiss_runtime
    for name, relative in [('torch', 'lib/libomp.dylib'), ('faiss', '.dylibs/libomp.dylib')]:
        lib = tmp_path / name / relative
        lib.parent.mkdir(parents=True)
        lib.write_bytes(b'conflicting native runtime')
    monkeypatch.setattr(faiss_runtime.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(faiss_runtime.importlib.util, 'find_spec',
                        lambda name: SimpleNamespace(origin=str(tmp_path / name / '__init__.py')))
    assert faiss_runtime.load_faiss() is None


def test_backup_preserves_face_model_contract(app_services):
    import zipfile

    from backend.ops.backup import export_backup, verify_archive
    s = app_services
    contract = s.config.data_dir / 'face-model.json'
    contract.write_text('{"embedding":{"sha256":"tested-model","dimension":512}}')
    exported = export_backup(s.db, s.config.data_dir)
    archive = s.config.data_dir / 'exports' / exported['name']
    verified = verify_archive(archive)
    assert 'face-model.json' in verified['files']
    with zipfile.ZipFile(archive) as z:
        assert z.read('face-model.json') == contract.read_bytes()


def test_exact_fallback_ignores_orphan_and_rejected_vectors(app_services):
    s = app_services
    mids = add_media(s.db, 1)
    # Orphan vectors deliberately have a higher score than the current row.
    s.store.append_many([vector()] * 300)
    current = normalize(vector() + vector(1))
    fid = insert_face(s.db, s.store, current, media=mids[0])
    rejected = insert_face(s.db, s.store, vector(), media=mids[0])
    with s.db.connect() as c:
        c.execute("UPDATE faces SET review_state='rejected' WHERE id=?", (rejected,))
    keys, scores = s.search._face_bruteforce(vector(), 1)
    assert keys.tolist() == [fid] and scores[0] == pytest.approx(2**-.5)


def test_corrupt_query_embedding_returns_search_warning(client, app_services):
    s = app_services
    ids = add_media(s.db, 1)
    fid = insert_face(s.db, s.store, vector(), media=ids[0])
    with s.db.connect() as c:
        c.execute("UPDATE faces SET embedding_sha='invalid' WHERE id=?", (fid,))
    result = client.post('/api/search/hybrid', json={'similar_face_id': fid})
    assert result.status_code == 200
    assert 'corrupt' in ' '.join(result.json()['warnings'])


def test_shutdown_during_background_index_sync_keeps_sources_recoverable(tmp_path, monkeypatch):
    import threading
    db = Database(tmp_path / 'index.sqlite'); add_media(db, 1)
    with EmbeddingStore(tmp_path / 'embeddings.bin') as store:
        fid = insert_face(db, store, vector())
        spaces = VectorSpaces(db, tmp_path)
        space = spaces.get(FACE_ARCFACE.key)
        started, release = threading.Event(), threading.Event()
        original_fold = space._fold
        results = []

        def slow_fold(rows):
            started.set()
            assert release.wait(5)
            original_fold(rows)

        monkeypatch.setattr(space, '_fold', slow_fold)
        sync = threading.Thread(target=lambda: results.append(space.sync()))
        sync.start(); assert started.wait(5)
        closer = threading.Thread(target=spaces.close)
        closer.start(); assert space._closing.wait(5)
        release.set(); sync.join(5); closer.join(5)
        assert not sync.is_alive() and not closer.is_alive()
        assert results[0]['action'] == 'cancelled'
        fresh = VectorSpaces(db, tmp_path)
        recovered = fresh.get(FACE_ARCFACE.key)
        recovered.ensure_ready()
        assert recovered.ann.search(vector(), 1)[0][0] == fid
        fresh.close()


def test_calibration_with_no_predictive_threshold_leaves_setting_unchanged(client, app_services):
    s = app_services
    seed_faces(s, 40)
    with s.db.connect() as c:
        c.execute("UPDATE faces SET manual=1,similarity=CASE WHEN id<=20 THEN .1 ELSE .7 END, "
                  "review_state=CASE WHEN id<=20 THEN 'confirmed' ELSE 'rejected' END")
        c.execute("INSERT INTO face_match_feedback(face_id,person_id,similarity,correct) "
                  "SELECT id,person_id,similarity,id<=20 FROM faces")
    before = s.db.settings()['matching_threshold']
    assert autotune(s.db) is None
    assert s.db.settings()['matching_threshold'] == before


def test_exact_repeated_video_frames_reuse_inference_and_keep_timestamps(app_services, tmp_path, monkeypatch):
    import sqlite3

    from backend import worker as module
    from backend.engine import Detections
    s = app_services
    image = np.zeros((64, 64, 3), np.uint8)
    changed = image.copy(); changed[0, 0, 0] = 1
    calls = []
    monkeypatch.setattr(s.engine, 'detect', lambda image: (calls.append(1), Detections([
        {'bbox': [10, 10, 30, 30], 'embedding': vector(), 'detection': .9, 'quality': .8}]))[1])
    monkeypatch.setattr(module.metadata_mod, 'extract', lambda *_: {})
    monkeypatch.setattr(module.media_io, 'video_metadata', lambda *_: {'duration': 9})
    monkeypatch.setattr(module.media_io, 'video_frames', lambda *_, **__: iter([(0, image), (3, image), (6, changed), (9, changed)]))
    monkeypatch.setattr(s.worker, '_refine_track_edges', lambda *_, **__: None)
    monkeypatch.setattr(s.worker, 'on_video_progress', None)
    path = tmp_path / 'fake.mp4'; path.write_bytes(b'fixture')
    with sqlite3.connect(':memory:') as staged:
        staged.execute('CREATE TABLE detections(id INTEGER PRIMARY KEY,bbox TEXT,timestamp REAL,detection REAL,'
                       'embedding_offset INTEGER,embedding_sha TEXT,jpeg BLOB,quality REAL,track_id INTEGER,landmarks TEXT)')
        s.worker._stage(path, 'video', s.db.settings(), staged)
        rows = staged.execute('SELECT timestamp,embedding_offset,track_id FROM detections ORDER BY id').fetchall()
    assert len(calls) == 2  # Exactly repeated frames reuse; a one-pixel change runs inference.
    assert [r[0] for r in rows] == [0, 3, 6, 9]
    assert rows[0][1] == rows[1][1] and rows[2][1] == rows[3][1] != rows[0][1]
    assert len({r[2] for r in rows}) == 1


def test_direct_media_inserts_do_not_replay_already_indexed_journal(tmp_path):
    from tests.helpers.fixtures import FAKE_VISUAL, fake_vector
    db = Database(tmp_path / 'index.sqlite'); mids = add_media(db, 3)
    spaces = VectorSpaces(db, tmp_path)
    try:
        s = spaces.register(FAKE_VISUAL); s.ensure_ready()
        s.add([(mid, fake_vector(mid)) for mid in mids])
        dirty = s.ann.dirty
        assert s.ann.meta['change_sequence'] == s._change_sequence()
        assert s.sync()['updated'] == 0 and s.ann.dirty <= dirty
        s.remove([mids[0]])
        assert s.ann.meta['change_sequence'] == s._change_sequence()
        assert s.sync()['updated'] == 0
    finally:
        spaces.close()


def test_direct_insert_consumes_other_process_changes_without_losing_updates(tmp_path):
    from tests.helpers.fixtures import FAKE_VISUAL, fake_vector
    db = Database(tmp_path / 'index.sqlite'); mids = add_media(db, 3)
    spaces, other = VectorSpaces(db, tmp_path), VectorSpaces(db, tmp_path)
    try:
        first = spaces.register(FAKE_VISUAL); first.ensure_ready()
        first.add([(mids[0], fake_vector(1))])
        writer = other.get(FAKE_VISUAL.key)
        writer.add([(mids[0], fake_vector(9)), (mids[1], fake_vector(2))])
        first.add([(mids[2], fake_vector(3))])
        assert first.ann.search(fake_vector(9), 1)[0][0] == mids[0]
        assert first.ann.search(fake_vector(2), 1)[0][0] == mids[1]
        assert first.ann.meta['change_sequence'] == first._change_sequence()
    finally:
        spaces.close(); other.close()


def test_new_face_is_folded_once_from_journal(tmp_path, monkeypatch):
    db = Database(tmp_path / 'index.sqlite'); add_media(db, 1)
    with EmbeddingStore(tmp_path / 'embeddings.bin') as store:
        spaces = VectorSpaces(db, tmp_path)
        space = spaces.get(FACE_ARCFACE.key); space.ensure_ready()
        calls, original = [], space.ann.upsert

        def counted(keys, vectors):
            calls.extend(keys)
            original(keys, vectors)

        monkeypatch.setattr(space.ann, 'upsert', counted)
        fid = insert_face(db, store, vector())
        result = space.sync()
        assert calls == [fid] and result['added'] == 1 and result['updated'] == 1
        spaces.close()


def test_incremental_threshold_and_ambiguous_candidates(tmp_path):
    """Singleton matches respect the floor; close identities stay unassigned."""
    db = Database(tmp_path / 'index.sqlite')
    add_media(db, 3)
    with EmbeddingStore(tmp_path / 'embeddings.bin') as store:
        with db.connect() as c:
            first = c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
        insert_face(db, store, vector(), first, 1)
        cluster = Clustering(db, store)
        query = vector() * .50 + vector(1) * np.sqrt(1 - .50 ** 2)
        with db.connect() as c:
            # Previously rejected because singleton threshold was raised to .52.
            pid, score = cluster.assign(c, query, media_id=3, threshold=.48)
            assert pid == first and score == pytest.approx(.50)
        with db.connect() as c:
            second = c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
        insert_face(db, store, normalize(vector() + vector(1) * .02), second, 2)
        cluster.invalidate()
        with db.connect() as c:
            pid, score = cluster.assign(c, vector(), media_id=3, threshold=.48)
            assert pid is None and score > .99
            assert c.execute('SELECT COUNT(*) FROM people').fetchone()[0] == 2


def test_broad_cluster_cannot_relax_configured_threshold(tmp_path):
    db = Database(tmp_path / 'index.sqlite')
    add_media(db, 3)
    with EmbeddingStore(tmp_path / 'embeddings.bin') as store:
        with db.connect() as c:
            person = c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
        for sign in (-1, 1):
            insert_face(db, store, normalize(vector() * .3 + vector(1) * sign), person, 1)
        query = normalize(vector() * .46 + vector(2) * np.sqrt(1 - .46 ** 2))
        with db.connect() as c:
            pid, score = Clustering(db, store).assign(c, query, media_id=3, threshold=.48)
            assert pid != person and score is None


def test_video_track_switch_stays_unassigned_and_unconfirmed(app_services, monkeypatch):
    import sqlite3
    import backend.worker as worker_module
    s = app_services
    media = add_media(s.db, 1, kind='video')[0]
    monkeypatch.setattr(worker_module.media_io, 'write_thumbnail', lambda *args: None)
    monkeypatch.setattr(s.worker, '_checkpoint', lambda: None)
    with sqlite3.connect(':memory:') as staged:
        staged.row_factory = sqlite3.Row
        staged.execute('CREATE TABLE detections(id INTEGER PRIMARY KEY,bbox TEXT,timestamp REAL,detection REAL,'
                       'embedding_offset INTEGER,embedding_sha TEXT,jpeg BLOB,face_id INTEGER,'
                       'quality REAL,track_id INTEGER,landmarks TEXT)')
        # An identity switch within one tracker ID must not inherit the leader.
        for i, v in enumerate((vector(), vector(), vector(1))):
            off, sha = s.store.append(v)
            staged.execute("INSERT INTO detections(id,bbox,timestamp,detection,embedding_offset,embedding_sha,"
                           "jpeg,quality,track_id) VALUES (?,'[0,0,20,20]',?,.9,?,?,X'00',?,1)",
                           (i + 1, float(i), off, sha, .9 - i * .1))
        s.worker._publish(media, staged, {'width': 100, 'height': 100, 'meta_version': 1}, b'', 0, s.db.settings())
    rows = s.db.all('SELECT person_id,review_state FROM faces ORDER BY timestamp')
    assert rows[0]['person_id'] == rows[1]['person_id']
    assert rows[2]['person_id'] is None
    assert all(row['review_state'] == 'unreviewed' for row in rows)


def test_coreml_runtime_failure_retries_same_frame_on_cpu(tmp_path, monkeypatch):
    import threading
    engine = Engine(SimpleNamespace(model_dir=tmp_path, data_dir=tmp_path))
    engine._provider = 'CoreMLExecutionProvider'
    sessions = []
    for name in ('_detector', '_recognizer'):
        session = SimpleNamespace(set_providers=lambda providers: sessions.append(providers))
        setattr(engine, name, SimpleNamespace(session=session))
    calls = []
    def inference(image, multi_scale):
        calls.append(image)
        if len(calls) == 1:
            raise RuntimeError('CoreML static output shape and inferred shape have different ranks')
        return ['same-frame-result']
    monkeypatch.setattr(engine, '_detect_once', inference)
    frame = np.zeros((240,320,3), np.uint8)
    assert engine.detect(frame) == ['same-frame-result']
    assert calls[0] is calls[1]
    assert sessions == [['CPUExecutionProvider'], ['CPUExecutionProvider']]
    assert engine._provider == 'CPUExecutionProvider'


def test_unrelated_inference_errors_are_not_hidden(tmp_path, monkeypatch):
    engine = Engine(SimpleNamespace(model_dir=tmp_path, data_dir=tmp_path))
    engine._provider = 'CoreMLExecutionProvider'
    def inference(*args):
        raise ValueError('Invalid image')
    monkeypatch.setattr(engine, '_detect_once', inference)
    with pytest.raises(ValueError, match='Invalid image'):
        engine.detect(np.zeros((20,20,3), np.uint8))


def test_video_reentry_reuses_person_but_respects_rejections(tmp_path):
    db=Database(tmp_path/'index.sqlite'); add_media(db,2,kind='video')
    with EmbeddingStore(tmp_path/'embeddings.bin') as store:
        with db.connect() as c:
            pid=c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
        # A broad historical centroid differs from this video's clear appearance.
        insert_face(db,store,vector(1),pid,1)
        query=insert_face(db,store,vector(),None,2)
        memory={pid:[(vector(),.9)]}
        with db.connect() as c:
            c.execute('UPDATE faces SET quality=.9,timestamp=30 WHERE id=?',(query,))
            cluster=Clustering(db,store)
            assigned,score=cluster.assign(c,vector(),face_id=query,video_identities=memory)
            assert assigned==pid and score==pytest.approx(1)
        with db.connect() as c:
            c.execute('INSERT INTO rejections(face_id,person_id) VALUES (?,?)',(query,pid))
            assigned,_=cluster.assign(c,vector(),face_id=query,video_identities=memory)
            assert assigned!=pid


def test_video_identity_memory_does_not_merge_different_people(tmp_path):
    db=Database(tmp_path/'index.sqlite'); add_media(db,2,kind='video')
    with EmbeddingStore(tmp_path/'embeddings.bin') as store:
        with db.connect() as c: pid=c.execute('INSERT INTO people DEFAULT VALUES').lastrowid
        insert_face(db,store,vector(),pid,1)
        fid=insert_face(db,store,vector(1),None,2)
        with db.connect() as c:
            c.execute('UPDATE faces SET quality=.9 WHERE id=?',(fid,))
            other,_=Clustering(db,store).assign(c,vector(1),face_id=fid,video_identities={pid:[(vector(),.9)]})
            assert other!=pid
