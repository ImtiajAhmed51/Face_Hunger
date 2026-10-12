"""Real FFmpeg playback copies: originals untouched, cache survives restart."""
import hashlib
import subprocess
import time

import pytest
from backend.media_processing import _ffmpeg_bin
from backend.video_compat import VideoCompatService, ProbeInfo


@pytest.mark.parametrize('extension,codec', [('avi', 'mpeg4'), ('mpg', 'mpeg2video'), ('wmv', 'wmv2')])
def test_legacy_video_cache(tmp_path, monkeypatch, extension, codec):
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        pytest.skip('FFmpeg unavailable')
    source = tmp_path / f'original.{extension}'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=128x96:rate=25',
                    '-t', '0.4', '-c:v', codec, str(source)], check=True)
    before = (source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).digest())
    updates = []
    service = VideoCompatService(tmp_path / 'cache', on_replaced=lambda *args: updates.append(args))
    monkeypatch.setattr(service, '_pick_video_encoder', lambda *args: ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p'])
    assert service.needs_conversion(source)[0]
    service.ensure_playable(1, source)
    deadline = time.monotonic() + 30
    while service.get_state(1).status not in ('completed', 'failed') and time.monotonic() < deadline:
        time.sleep(0.05)
    state = service.get_state(1)
    assert state.ready, state.error
    cached = service.playback_path(1, source)
    assert cached.is_file() and service.probe(cached).browser_compatible()
    assert before == (source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).digest())
    assert not updates
    assert not list(tmp_path.glob('*.lfs_original'))
    fresh = VideoCompatService(tmp_path / 'cache')
    assert fresh.ensure_playable(1, source, start_if_needed=False)[0]
    source.touch()
    assert fresh.playback_path(1, source) != cached
    assert not fresh.ensure_playable(1, source, start_if_needed=False)[0]


def test_high_bit_depth_h264_is_not_native_or_remuxable():
    probe = ProbeInfo('mp4', 'h264', 'aac', 128, 96, 1, pix_fmt='yuv420p10le')
    assert not probe.browser_compatible()
    assert not probe.can_remux_to_mp4()


def test_cached_playback_endpoint_keeps_source_path(client, app_services, tmp_path, monkeypatch):
    from tests.helpers.fixtures import add_media
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        pytest.skip('FFmpeg unavailable')
    source = tmp_path / 'legacy.avi'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=128x96:rate=25',
                    '-t', '0.4', '-c:v', 'mpeg4', str(source)], check=True)
    add_media(app_services.db, 1)
    with app_services.db.connect() as conn:
        conn.execute("UPDATE media SET path=?,name='legacy.avi',kind='video',missing=0 WHERE id=1", (str(source),))
    svc = app_services.video_compat
    monkeypatch.setattr(svc, '_pick_video_encoder', lambda *args: ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p'])
    response = client.post('/api/media/1/convert', headers={'X-LFS-Request': '1'})
    assert response.status_code == 200
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = client.get('/api/media/1/playback').json()
        if status['ready'] or status['status'] == 'failed':
            break
        time.sleep(0.05)
    assert status['ready'], status
    playback = client.get('/api/media/1/file')
    assert playback.status_code == 200
    assert playback.headers['content-type'].startswith('video/mp4')
    assert playback.content == svc.playback_path(1, source).read_bytes()
    assert client.get('/api/media/1/file', headers={'Range': 'bytes=0-15'}).status_code == 206
    assert app_services.db.one('SELECT path FROM media WHERE id=1')['path'] == str(source)
    assert source.is_file()
    assert status['has_playback_copy']
    copy = svc.playback_path(1, source)
    original_bytes = source.read_bytes()
    result = client.delete('/api/media/1/playback-copy', headers={'X-LFS-Request': '1'})
    assert result.status_code == 200
    assert result.json()['deleted'] == 1
    assert not copy.exists()
    assert source.read_bytes() == original_bytes
    after = client.get('/api/media/1/playback').json()
    assert not after['has_playback_copy'] and after['needs_conversion']
    assert not copy.exists()  # status polling cannot recreate it


def test_cleanup_lists_and_deletes_only_generated_playback_copies(client, app_services, tmp_path):
    from tests.helpers.fixtures import add_media
    add_media(app_services.db, 1)
    source = tmp_path / 'original.avi'
    source.write_bytes(b'original must survive')
    with app_services.db.connect() as conn:
        conn.execute('UPDATE media SET path=? WHERE id=1', (str(source),))
    cache = app_services.video_compat.work_dir
    cached = cache / ('1-' + 'a' * 24 + '.mp4')
    cached.write_bytes(b'generated copy')
    other = cache / 'unrelated.mp4'
    other.write_bytes(b'keep')
    data = client.get('/api/cleanup').json()
    assert any(i['path'] == str(cached) for i in data['converted_backups'])
    assert not any(i.get('original_path') == str(cached) for i in data['soft_originals'])
    assert client.post('/api/media/converted-backups/delete', json={'path': str(source)}).status_code == 404
    assert client.post('/api/media/converted-backups/delete', json={'path': str(other)}).status_code == 404
    assert client.post('/api/media/converted-backups/delete', json={'path': str(cached)}).status_code == 200
    assert not cached.exists()
    assert source.read_bytes() == b'original must survive'
    assert other.exists()

    cached.write_bytes(b'regenerated playback copy')
    legacy = tmp_path / 'original.mp4.lfs_converted'
    legacy.write_bytes(b'older converted backup')
    result = client.post('/api/media/converted-backups/purge', json={})
    assert result.status_code == 200
    assert result.json()['deleted'] == 2
    assert not cached.exists() and not legacy.exists()
    assert source.read_bytes() == b'original must survive'
    assert other.exists()


def test_grid_badges_exclude_native_ready_and_track_copies(client, app_services, tmp_path):
    from tests.helpers.fixtures import add_media
    add_media(app_services.db, 1)
    source = tmp_path / 'native.mp4'
    source.write_bytes(b'fixture')
    with app_services.db.connect() as conn:
        conn.execute("UPDATE media SET path=?,kind='video',missing=0 WHERE id=1", (str(source),))
    app_services.video_compat._set_state(1, status='completed', ready=True, mode='native')
    assert client.get('/api/media/conversion-statuses?ids=1').json()['items'] == {}
    source = source.rename(tmp_path / 'legacy.avi')
    with app_services.db.connect() as conn:
        conn.execute('UPDATE media SET path=? WHERE id=1', (str(source),))
    assert client.get('/api/media/conversion-statuses?ids=1').json()['items']['1']['status'] == 'needs_conversion'
    copied = app_services.video_compat.playback_path(1, source)
    copied.write_bytes(b'copy')
    assert client.get('/api/media/conversion-statuses?ids=1').json()['items']['1']['status'] == 'completed'
    copied.unlink()
    assert client.get('/api/media/conversion-statuses?ids=1').json()['items']['1']['status'] == 'needs_conversion'
