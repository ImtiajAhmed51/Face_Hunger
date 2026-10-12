import json
from pathlib import Path
import sqlite3

import numpy as np
import pytest

from backend.engine import Detections
from backend.video.checkpoint import VideoCheckpoint, get, source_fingerprint
from tests.test_face_pipeline import vector


def prepare(s,tmp_path,monkeypatch):
    import backend.worker as module
    path=tmp_path/'video.mp4';path.write_bytes(b'original bytes')
    calls=[]
    monkeypatch.setattr(s.engine,'detect',lambda im:(calls.append(int(im[0,0,0])) or Detections([
        dict(bbox=[10,10,30,30],embedding=vector(),detection=.9,quality=.8)])))
    monkeypatch.setattr(module.metadata_mod,'extract',lambda *a:{'meta_version':1})
    monkeypatch.setattr(module.media_io,'video_metadata',lambda *a:{'duration':12,'width':64,'height':64})
    monkeypatch.setattr(s.worker,'_refine_track_edges',lambda *a:None)
    monkeypatch.setattr(s.worker,'on_video_progress',None)
    monkeypatch.setattr(module.dup_mod,'content_hash',lambda *a:'original-hash')
    monkeypatch.setattr(module.dup_mod,'video_phash',lambda *a,**kw:None)
    return module,path,calls


def test_interrupted_stage_restores_without_repeating_inference(app_services,tmp_path,monkeypatch):
    s=app_services;module,path,calls=prepare(s,tmp_path,monkeypatch)
    starts=[];fail=[True]
    def frames(*a,resume_state=None,**kw):
        start=resume_state.get('next_sample',0);starts.append(start)
        for i in range(4):
            if i*3<start:continue
            if fail[0] and i==2:raise RuntimeError('power interruption')
            im=np.zeros((64,64,3),np.uint8);im[0,0,0]=i
            resume_state['next_sample']=(i+1)*3
            yield i*3,im
    monkeypatch.setattr(module.media_io,'video_frames',frames)
    settings=s.db.settings();key=source_fingerprint(path,path.stat(),settings,s.engine,s.store)
    cache=VideoCheckpoint(s.config.data_dir,999,key)
    with cache as staged:
        with pytest.raises(RuntimeError,match='interruption'):s.worker._stage(path,'video',settings,staged)
        assert staged.execute('SELECT COUNT(*) FROM detections').fetchone()[0]==2
    fail[0]=False
    with VideoCheckpoint(s.config.data_dir,999,key) as staged:
        metadata,_,_=s.worker._stage(path,'video',settings,staged)
        rows=staged.execute('SELECT timestamp,track_id FROM detections ORDER BY id').fetchall()
        assert [r[0] for r in rows]==[0,3,6,9]
        assert len(set(r[1] for r in rows))==1
        assert metadata['_profile']['checkpoint_faces_reused']==2
        s.worker._stage(path,'video',settings,staged)
    assert starts==[0,6] and calls==[0,1,2,3]
    assert path.read_bytes()==b'original bytes'


def test_configuration_change_invalidates_only_generated_staging(app_services,tmp_path,monkeypatch):
    s=app_services;_,path,_=prepare(s,tmp_path,monkeypatch)
    settings=s.db.settings();key=source_fingerprint(path,path.stat(),settings,s.engine,s.store)
    with VideoCheckpoint(s.config.data_dir,999,key) as c:
        c.execute("INSERT INTO detections(bbox) VALUES ('cached')");c.commit()
    changed=dict(settings,video_interval=6)
    key2=source_fingerprint(path,path.stat(),changed,s.engine,s.store)
    with VideoCheckpoint(s.config.data_dir,999,key2) as c:
        assert c.execute('SELECT COUNT(*) FROM detections').fetchone()[0]==0
    assert path.read_bytes()==b'original bytes'


def test_corrupted_checkpoint_regenerates_without_touching_media(tmp_path):
    cache=VideoCheckpoint(tmp_path,1,{'version':1})
    cache.path.write_bytes(b'corrupt sqlite')
    with cache as c: assert get(c,'fingerprint')=={'version':1}


def test_index_retry_publishes_once_and_removes_checkpoint(app_services,tmp_path,monkeypatch):
    s=app_services;module,path,calls=prepare(s,tmp_path,monkeypatch)
    fail=[True]
    def frames(*a,resume_state=None,**kw):
        for i in range(4):
            if i*3<resume_state.get('next_sample',0):continue
            if fail[0] and i==2:raise RuntimeError('interrupted')
            im=np.zeros((64,64,3),np.uint8);im[0,0,0]=i
            resume_state['next_sample']=(i+1)*3
            yield i*3,im
    monkeypatch.setattr(module.media_io,'video_frames',frames)
    with s.db.connect() as conn:
        lid=conn.execute("INSERT INTO libraries(path,name) VALUES (?,'test')",(str(tmp_path),)).lastrowid
    library=s.db.one('SELECT * FROM libraries WHERE id=?',(lid,))
    args=(library,tmp_path,path,'video',s.db.settings(),False,False)
    with pytest.raises(RuntimeError,match='interrupted'):s.worker._index_file(*args)
    assert s.db.one('SELECT COUNT(*) AS n FROM faces')['n']==0
    fail[0]=False
    assert s.worker._index_file(*args)[0]=='indexed'
    assert s.db.one('SELECT COUNT(*) AS n FROM faces')['n']==4
    assert not list((s.config.data_dir/'indexing_checkpoints').glob('*.sqlite'))
    assert s.worker._index_file(*args)[0]=='skipped'
    assert calls==[0,1,2,3]
    assert path.read_bytes()==b'original bytes'


@pytest.mark.parametrize('adaptive',[False,True])
def test_real_video_sampler_resume_preserves_frames(tmp_path,adaptive):
    import hashlib
    from backend.media_processing import video_frames
    from tests.helpers.video_fixture import make_video
    path=make_video(tmp_path/'resume.mp4')
    def digest(frame):return hashlib.sha256(memoryview(frame)).hexdigest()
    expected=[(round(t,4),digest(im)) for t,im in video_frames(path,3,adaptive=adaptive)]
    state={}; iterator=video_frames(path,3,adaptive=adaptive,resume_state=state)
    prefix=[]
    for _ in range(4):
        t,im=next(iterator);prefix.append((round(t,4),digest(im)))
    iterator.close()
    suffix=[(round(t,4),digest(im)) for t,im in video_frames(path,3,adaptive=adaptive,resume_state=state)]
    assert prefix+suffix==expected
