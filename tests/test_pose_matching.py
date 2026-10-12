"""Disposable tests for conservative multi-view incremental matching."""
import numpy as np
import pytest
from backend.embeddings import normalize
from tests.test_face_pipeline import vector,insert_face
from tests.helpers.fixtures import add_media

@pytest.mark.parametrize('mode',['supported','single_view','excluded','collision','low_quality','reload'])
def test_pose_matching_keeps_corrections_and_requires_support(app_services,mode):
 s=app_services
 for mid in (1,2):add_media(s.db,mid)
 with s.db.connect() as c:
  pid=c.execute('INSERT INTO people(face_count) VALUES (0)').lastrowid
 for _ in range(40):insert_face(s.db,s.store,vector(),pid)
 refs=[normalize(.1*vector()+vector(1)+.2*vector(2)),normalize(.1*vector()+vector(1)-.2*vector(2))]
 for v in refs[:1 if mode=='single_view' else 2]:
  fid=insert_face(s.db,s.store,v,pid)
  with s.db.connect() as c:c.execute('UPDATE faces SET quality=.8 WHERE id=?',(fid,))
 query=normalize(.4*vector()+.9165*vector(1))
 qid=insert_face(s.db,s.store,query,media=2)
 with s.db.connect() as c:
  c.execute('UPDATE faces SET quality=? WHERE id=?',(.3 if mode=='low_quality' else .8,qid))
  if mode=='excluded':c.execute('INSERT INTO exclusions(media_id,person_id) VALUES (2,?)',(pid,))
  if mode=='collision':c.execute('UPDATE faces SET media_id=2 WHERE id=?',(fid,))
 s.cluster.invalidate()
 with s.db.connect() as c:
  s.cluster._load_cache(c)
  assert float(s.cluster._cache[pid][0]@query)<.48
  result,_=s.cluster.assign(c,query,face_id=qid)
  assert (result==pid)==(mode in ('supported','reload'))
  if mode=='reload':
   c.execute('UPDATE faces SET person_id=? WHERE id=?',(result,qid))
 s.cluster.invalidate()
 if mode=='reload':
  from backend.clustering import Clustering
  restarted=Clustering(s.db,s.store)
  with s.db.connect() as c:
   restarted._load_cache(c)
   assert pid in restarted._pose_candidates(query,.48)

def test_duplicate_views_do_not_create_independent_support():
 from backend.clustering import Clustering
 refs={}
 for _ in range(30):Clustering._remember_reference(refs,1,vector(),.8)
 assert len(refs[1])==1
 for i in range(1,12):Clustering._remember_reference(refs,1,vector(i),.8)
 assert len(refs[1])==6
