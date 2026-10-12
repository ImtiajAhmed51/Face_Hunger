"""Redesign regressions use disposable metadata, never the user's live library."""
import json

from tests.helpers.fixtures import add_media


def seed_faces(services, n=130):
    add_media(services.db, 2)
    with services.db.connect() as conn:
        conn.execute("INSERT INTO people(id,name,face_count) VALUES (1,'Ada',?)", (n,))
        conn.executemany("INSERT INTO faces(id,media_id,person_id,bbox,detection,embedding_offset,embedding_sha,review_state) "
                         "VALUES (?,1,1,'[0,0,10,10]',0.9,0,'unused',?)",
                         [(i, ['unreviewed', 'confirmed', 'rejected'][i % 3]) for i in range(1, n + 1)])


def test_face_pages_are_bounded_and_include_all_video_tracks(client, app_services):
    seed_faces(app_services)
    with app_services.db.connect() as conn:
        conn.execute("UPDATE media SET kind='video' WHERE id=1")
        conn.execute("UPDATE faces SET track_id=1,timestamp=id*1.0")
    first = client.get('/api/people/1/faces?limit=60').json()
    second = client.get('/api/people/1/faces?page=2&limit=60').json()
    assert first['total'] == 130
    assert len(first['items']) == len(second['items']) == 60
    assert not {f['id'] for f in first['items']} & {f['id'] for f in second['items']}
    assert all(f['display_name'] == 'Ada' for f in first['items'])
    assert client.get('/api/people/404/faces').status_code == 404


def test_deleted_faces_include_confirmed_rejected_and_missing_sources(client, app_services):
    seed_faces(app_services, 3)
    with app_services.db.connect() as conn:
        conn.execute("UPDATE faces SET deleted_at='2026-10-10'")
        conn.execute("UPDATE media SET missing=1 WHERE id=1")
    result = client.get('/api/review?deleted=true').json()
    assert result['total'] == 3
    assert {f['review_state'] for f in result['items']} == {'confirmed', 'rejected', 'unreviewed'}
    assert client.get('/api/people/1/faces?deleted=true').json()['total'] == 3


def test_bulk_review_is_atomic_idempotent_and_does_not_read_embeddings(client, app_services, monkeypatch):
    seed_faces(app_services)
    def no_embeddings(*args):
        raise AssertionError('A review must not recalculate embeddings')
    monkeypatch.setattr(app_services.store, 'read', no_embeddings)
    ids = list(range(1, 41))
    assert client.post('/api/faces/review', json={'face_ids': [3, 9999], 'decision': 'yes'}).status_code == 404
    assert app_services.db.one('SELECT review_state FROM faces WHERE id=3')['review_state'] == 'unreviewed'
    assert client.post('/api/faces/review', json={'face_ids': ids, 'decision': 'yes'}).json()['ok']
    assert client.post('/api/faces/review', json={'face_ids': ids + ids, 'decision': 'yes'}).json()['already']
    assert client.post('/api/faces/review', json={'face_ids': ids, 'decision': 'no'}).status_code == 200
    assert app_services.db.one('SELECT COUNT(*) c FROM rejections')['c'] == 40
    assert client.post('/api/faces/review', json={'face_ids': ids, 'decision': 'reset'}).status_code == 200
    assert app_services.db.one('SELECT COUNT(*) c FROM rejections')['c'] == 0
    assert client.post('/api/faces/review', json={'face_ids': [], 'decision': 'yes'}).status_code == 422
    assert client.post('/api/faces/review', json={'face_ids': ids, 'decision': 'invalid'}).status_code == 400
    assert json.loads(app_services.db.one("SELECT value FROM settings WHERE key='review_autotune_counter'")['value']) > 0


def test_recently_added_uses_index_timestamp_not_capture_date(client, app_services):
    ids = add_media(app_services.db, 2)
    with app_services.db.connect() as conn:
        conn.execute("UPDATE media SET captured_at='2020-01-01',indexed_at='2026-10-10' WHERE id=?", (ids[0],))
        conn.execute("UPDATE media SET captured_at='2025-01-01',indexed_at='2026-10-09' WHERE id=?", (ids[1],))
    assert client.get('/api/media?sort=indexed').json()['items'][0]['id'] == ids[0]
    assert client.get('/api/media?sort=date').json()['items'][0]['id'] == ids[1]


def test_bulk_review_undo_preserves_prior_states_and_rejection_records(client, app_services):
    seed_faces(app_services, 3)
    client.post('/api/faces/3/review', json={'decision': 'no'})
    before = app_services.db.all('SELECT id,person_id,review_state,manual FROM faces ORDER BY id')
    rejected = app_services.db.all('SELECT * FROM rejections')
    r = client.post('/api/faces/review', json={'face_ids': [1,2,3], 'decision': 'yes'}).json()
    assert client.post(f"/api/audit/{r['audit_id']}/undo").status_code == 200
    assert app_services.db.all('SELECT id,person_id,review_state,manual FROM faces ORDER BY id') == before
    assert app_services.db.all('SELECT * FROM rejections') == rejected
    r = client.post('/api/faces/review', json={'face_ids': [1,2,3], 'decision': 'yes'}).json()
    client.post('/api/faces/3/review', json={'decision': 'no'})
    assert client.post(f"/api/audit/{r['audit_id']}/undo").status_code == 409


def test_folder_search_matches_both_listing_and_hybrid(client, app_services):
    add_media(app_services.db, 2, library_path='/fixture/holiday')
    assert client.get('/api/media?q=holiday').json()['total'] == 2
    assert client.post('/api/search/hybrid', json={'name': 'holiday'}).json()['total'] == 2


def test_shell_settings_skip_expensive_storage_scan(client, monkeypatch):
    from backend.routers import settings
    calls = []
    monkeypatch.setattr(settings, 'dir_usage', lambda path: (calls.append(path) or (123, 1)))
    lean = client.get('/api/settings?storage=false')
    assert lean.status_code == 200
    assert 'theme' in lean.json() and 'storage_bytes' not in lean.json()
    assert calls == []
    full = client.get('/api/settings').json()
    assert full['storage_bytes'] == {'database': 123, 'embeddings': 123, 'thumbnails': 123}
    assert len(calls) == 3


def test_module_entrypoint_uses_one_app_and_bounds_stream_shutdown(make_config, monkeypatch):
    import runpy
    from types import SimpleNamespace

    import uvicorn

    from backend import app, config
    from backend.ops import logging

    cfg = make_config(watch=False)
    instance = SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(config_warnings=[])))
    creations = []
    calls = []
    monkeypatch.setattr(config, 'Config', lambda: cfg)
    monkeypatch.setattr(app, 'create_app', lambda value: (creations.append(value) or instance))
    monkeypatch.setattr(logging, 'configure', lambda *args: None)
    monkeypatch.setattr(uvicorn, 'run', lambda value, **kwargs: calls.append((value, kwargs)))
    runpy.run_module('backend', run_name='__main__')
    assert creations == [cfg]
    assert len(calls) == 1 and calls[0][0] is instance
    assert calls[0][1]['timeout_graceful_shutdown'] == 5


def test_grid_previews_are_small_revision_cached_and_preserve_cached_source(client, app_services, monkeypatch):
    import io
    import os

    import numpy as np
    from PIL import Image

    from backend.services.thumbnails import _grid_preview

    add_media(app_services.db, 1)
    path = app_services.config.data_dir / 'thumbnails' / 'media-1.jpg'
    pixels = np.random.default_rng(2).integers(0, 256, (320, 480, 3), dtype=np.uint8)
    Image.fromarray(pixels).save(path, quality=90)
    before = path.read_bytes()
    url = '/api/media/1/thumbnail?size=384'
    first = client.get(url)
    assert first.status_code == 200 and first.headers['content-type'] == 'image/webp'
    assert Image.open(io.BytesIO(first.content)).size == (384, 256)
    assert len(first.content) < len(before)
    hits = _grid_preview.cache_info().hits
    assert client.get(url).content == first.content
    assert _grid_preview.cache_info().hits > hits
    assert path.read_bytes() == before
    assert client.get('/api/media/1/thumbnail').content == before
    monkeypatch.setattr(app_services.edits, 'get', lambda _: {'rotation': 90})
    assert Image.open(io.BytesIO(client.get(url).content)).size == (256, 384)
    monkeypatch.setattr(app_services.edits, 'get', lambda _: {})
    modified = path.stat().st_mtime_ns
    Image.new('RGB', (480, 320), 'red').save(path)
    os.utime(path, ns=(modified + 1, modified + 1))
    changed = client.get(url)
    assert changed.content != first.content
    assert client.get('/api/media/1/thumbnail?size=5000').status_code == 422
