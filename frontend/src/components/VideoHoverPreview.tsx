import { memo, useEffect, useRef, useState } from 'react';
import { Thumbnail } from './ui';

export const VideoHoverPreview = memo(function VideoHoverPreview({ id, name }: { id: number; name: string }) {
  const root = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const [hover, setHover] = useState(false);
  const [active, setActive] = useState(false);
  const [ready, setReady] = useState(false);
  const failed = useRef(false);
  useEffect(() => {
    if (!hover || failed.current || !window.matchMedia('(hover: hover) and (pointer: fine)').matches || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const timer = window.setTimeout(() => { if (!document.hidden) setActive(true); }, 400);
    return () => { clearTimeout(timer); setActive(false); };
  }, [hover]);
  useEffect(() => {
    const stop = () => { setHover(false); setActive(false); };
    const visibility = () => { if (document.hidden) stop(); };
    const observer = new IntersectionObserver(entries => { if (!entries[0]?.isIntersecting) stop(); });
    if (root.current) observer.observe(root.current);
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('blur', stop);
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', visibility); window.removeEventListener('blur', stop); };
  }, []);
  useEffect(() => {
    setReady(false);
    const player = video.current;
    if (!active || !player) return;
    // This endpoint serves only a capped, silent four-second clip, never the original.
    player.src = `/api/media/${id}/hover-preview`;
    void player.play().catch(() => { setActive(false); });
    return () => {
      player.pause();
      player.removeAttribute('src');
      player.load();
    };
  }, [active, id]);
  return <div ref={root} className="video-hover-preview" onPointerEnter={event => { if (event.pointerType === 'mouse') setHover(true); }} onPointerLeave={() => setHover(false)}>
    <Thumbnail src={`/api/media/${id}/thumbnail`} alt={name} icon="video" />
    {active && <video ref={video} className="video-hover-player" style={{ opacity: ready ? 1 : 0 }} muted playsInline loop preload="none" aria-hidden="true" tabIndex={-1}
      onPlaying={() => setReady(true)} onError={() => { failed.current = true; setActive(false); }} />}
  </div>;
});
