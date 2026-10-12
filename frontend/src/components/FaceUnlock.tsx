import { useEffect, useRef, useState } from 'react';
import { useT } from '../i18n';
import { mutate } from '../api';
import { Button } from './primitives/button';
import { ErrorNotice } from './ui';

export function FaceCamera({ enrollment = false, onCapture }: { enrollment?: boolean; onCapture: (images: string[]) => Promise<unknown> }) {
  const t = useT();
  const video = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const [open, setOpen] = useState(false);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void navigator.mediaDevices?.getUserMedia({ video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } }, audio: false }).then(s => {
      if (cancelled) { s.getTracks().forEach(t => t.stop()); return; }
      stream.current = s;
      if (video.current) video.current.srcObject = s;
    }).catch(() => { setError(t('faceLock.permission')); setOpen(false); });
    return () => { cancelled = true; stream.current?.getTracks().forEach(t => t.stop()); stream.current = null; setReady(false); };
  }, [open]);
  return <div className="face-unlock-camera">
    <p className="small-text muted">{t('faceLock.warning')}</p>
    {open && <video ref={video} autoPlay playsInline muted onLoadedData={() => setReady(true)} aria-label={t('faceLock.preview')} />}
    <ErrorNotice error={error} />
    <div className="inline-actions">
      {!open ? <Button type="button" className="button primary" onClick={() => {
        setError('');
        if (!navigator.mediaDevices?.getUserMedia) { setError(t('faceLock.secure')); return; }
        setOpen(true);
      }}>{t('faceLock.open')}</Button> : <>
        <Button type="button" className="button primary" disabled={!ready || busy} onClick={async () => {
          setBusy(true); setError('');
          try {
            const images: string[] = [];
            for (let i = 0; i < (enrollment ? 3 : 1); i++) {
              const v = video.current;
              if (!v?.videoWidth) throw new Error(t('faceLock.notReady'));
              const canvas = document.createElement('canvas');
              canvas.width = Math.min(640, v.videoWidth);
              canvas.height = Math.round(canvas.width * v.videoHeight / v.videoWidth);
              const ctx = canvas.getContext('2d');
              if (!ctx) throw new Error(t('faceLock.captureError'));
              ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
              images.push(canvas.toDataURL('image/jpeg', .85).split(',')[1]);
              if (enrollment) await new Promise(resolve => setTimeout(resolve, 350));
            }
            await onCapture(images);
            setOpen(false);
          } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
          finally { setBusy(false); }
        }}>{busy ? t('faceLock.busy') : enrollment ? t('faceLock.register') : t('faceLock.unlock')}</Button>
        <Button type="button" className="button" disabled={busy} onClick={() => setOpen(false)}>{t('faceLock.close')}</Button>
      </>}
    </div>
  </div>;
}

export function AdminFaceSettings({ enabled, faceEnabled, onDone }: { enabled: boolean; faceEnabled: boolean; onDone: () => void }) {
  const t = useT();
  const [password, setPassword] = useState('');
  const [again, setAgain] = useState('');
  const [error, setError] = useState('');
  return <div className="admin-face-settings">
    <h3>{t('faceLock.title')}</h3>
    <p className="muted small-text">{faceEnabled ? t('faceLock.registered') : t('faceLock.prompt')}</p>
    <label className="field">{enabled ? t('faceLock.current') : t('faceLock.create')}
      <input type="password" autoComplete={enabled ? 'current-password' : 'new-password'} minLength={8} value={password} onChange={e => setPassword(e.target.value)} />
    </label>
    {!enabled && <label className="field">{t('faceLock.repeat')}<input type="password" autoComplete="new-password" value={again} onChange={e => setAgain(e.target.value)} /></label>}
    {password.length >= 8 && (enabled || password === again) && <FaceCamera enrollment onCapture={async images => {
      await mutate('/lock/face/enroll', { password, images });
      setPassword(''); setAgain(''); onDone();
    }} />}
    {faceEnabled && <Button type="button" className="button danger" disabled={!password} onClick={async () => {
      setError('');
      try { await mutate('/lock/face', { password }, 'DELETE'); setPassword(''); onDone(); }
      catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    }}>{t('faceLock.remove')}</Button>}
    <ErrorNotice error={error} />
  </div>;
}
