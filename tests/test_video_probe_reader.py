import numpy as np
import pytest
from backend import media_processing as media


def test_probe_frames_share_decoder_and_close_on_cancel(monkeypatch):
    opens=[]; closes=[]; seeks=[]
    class Capture:
        def set(self, prop, ms): seeks.append(ms); return True
        def read(self): return True,np.zeros((24,32,3),np.uint8)
        def release(self): closes.append(1)
    monkeypatch.setattr(media,'_capture',lambda p:(opens.append(p) or Capture()))
    iterator=media.frames_at('fixture',[0,3,6])
    assert next(iterator)[0]==0
    assert next(iterator)[0]==3
    iterator.close()
    assert len(opens)==1 and len(closes)==1 and seeks==[0,3000]


def test_probe_decode_failure_retains_frame_at_fallback(monkeypatch):
    class Capture:
        def set(self,*args): return True
        def read(self): return False,None
        def release(self): pass
    monkeypatch.setattr(media,'_capture',lambda p:Capture())
    called=[]
    monkeypatch.setattr(media,'frame_at',lambda p,t,**kw:(called.append(t) or np.zeros((20,20,3),np.uint8)))
    assert len(list(media.frames_at('fixture',[2,5])))==2
    assert called==[2,5]


def test_checkpoint_failure_releases_probe_decoder(monkeypatch):
    closed=[]
    class Capture:
        def release(self): closed.append(True)
    monkeypatch.setattr(media,'_capture',lambda p:Capture())
    def cancel(): raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'):
        list(media.frames_at('fixture',[0],cancel))
    assert closed==[True]
