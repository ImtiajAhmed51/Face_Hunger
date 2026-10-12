import json

from backend.merge_evidence import suggestions
from tests.helpers.fixtures import add_media
from tests.test_face_pipeline import insert_face, vector


def seed(s, quality=.9, same_media=False):
    media = add_media(s.db, 4)
    with s.db.connect() as c:
        c.execute('UPDATE media SET width=100,height=100')
        ids = [c.execute('INSERT INTO people DEFAULT VALUES').lastrowid for _ in range(2)]
    for k, pid in enumerate(ids):
        for j in range(2):
            fid = insert_face(s.db, s.store, vector(), pid, media[k*2] if same_media else media[k*2+j])
            with s.db.connect() as c:
                c.execute('UPDATE faces SET bbox=?,landmarks=?,quality=? WHERE id=?',
                          (json.dumps([10,10,60,70]), json.dumps([[25,30],[55,30],[40,45],[28,65],[52,65]]), quality, fid))
    with s.db.connect() as c:
        s.cluster.refresh(c)
    return ids, media


def test_reliable_independent_crops_support_suggestion(app_services):
    s=app_services
    seed(s)
    pairs=suggestions(s.db,s.store,s.cluster,.48)
    assert len(pairs)==1 and pairs[0]['evidence']=={'a_media':2,'b_media':2}


def test_blurry_or_background_crops_do_not_create_merge_suggestions(app_services):
    s=app_services
    seed(s,quality=.4)
    assert suggestions(s.db,s.store,s.cluster,.48)==[]
    with s.db.connect() as c:
        c.execute("UPDATE faces SET quality=.9,landmarks='[[0,0],[0,0],[0,0],[0,0],[0,0]]'")
    assert suggestions(s.db,s.store,s.cluster,.48)==[]


def test_video_repeats_cannot_supply_independent_evidence(app_services):
    s=app_services
    seed(s,same_media=True)
    assert suggestions(s.db,s.store,s.cluster,.48)==[]


def test_separation_and_cooccurrence_block_similar_people(app_services):
    s=app_services
    ids,media=seed(s)
    with s.db.connect() as c:
        c.execute('INSERT INTO separate_people VALUES (?,?)',tuple(ids))
    assert suggestions(s.db,s.store,s.cluster,.48)==[]
    with s.db.connect() as c:
        c.execute('DELETE FROM separate_people')
        c.execute('UPDATE faces SET media_id=? WHERE id=(SELECT MIN(id) FROM faces WHERE person_id=?)',(media[0],ids[1]))
    assert suggestions(s.db,s.store,s.cluster,.48)==[]
