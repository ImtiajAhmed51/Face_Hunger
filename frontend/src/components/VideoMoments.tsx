import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { request, timeLabel } from "../api";
import { useResource } from "../hooks";
import { useT } from "../i18n";
import { MediaViewer } from "./MediaViewer";
import { Thumbnail } from "./ui";

interface Moment { keyframe_id: number; media_id: number; name: string; t: number; similarity: number }

/** Scenes in videos matching a text query (SigLIP 2 over keyframes); click to play from there. */
export function MomentResults({ text, people }: { text: string; people?: number[] }) {
  const t = useT();
  const [open, setOpen] = useState<Moment | null>(null);
  const query = useQuery({
    queryKey: ["moments", text, people ?? []],
    queryFn: ({ signal }) => request<{ items: Moment[]; warning: string | null }>("/search/moments",
      { method: "POST", body: { text, people: people ?? [], limit: 24 }, signal }),
    enabled: text.trim().length > 0,
  });
  const items = query.data?.items ?? [];
  if (!items.length) return null;
  return (
    <section className="collection" aria-labelledby="moments-heading">
      <div className="section-heading"><div><h2 id="moments-heading">{t("video.moments")}</h2><p>{t("video.momentsHelp")}</p></div></div>
      <div className="moment-grid">
        {items.map((m) => (
          <button key={m.keyframe_id} className="moment-card" onClick={() => setOpen(m)}
            aria-label={`${m.name}, ${t("video.seek", { time: timeLabel(m.t) })}`}>
            <img src={`/api/keyframes/${m.keyframe_id}/image`} alt="" loading="lazy" />
            <span className="small-text"><strong>{timeLabel(m.t)}</strong> · <span className="muted">{m.name}</span></span>
          </button>
        ))}
      </div>
      {open && <MediaViewer id={open.media_id} timestamp={open.t} onClose={() => setOpen(null)} />}
    </section>
  );
}

interface PersonMoment { media_id: number; name: string; best_face_id: number; segments: { start: number; end: number }[] }

/** Every video a person appears in, with timestamp chips that open the player at that moment. */
export function PersonVideoMoments({ personId }: { personId: number }) {
  const t = useT();
  const data = useResource<{ items: PersonMoment[] }>(`/people/${personId}/moments`);
  const [open, setOpen] = useState<{ id: number; t: number } | null>(null);
  const items = data.data?.items ?? [];
  if (!items.length) return null;
  return (
    <section className="settings-section" aria-labelledby={`pm-${personId}`}>
      <div className="section-heading"><div><h2 id={`pm-${personId}`}>{t("video.personMoments")}</h2></div></div>
      <ul className="person-moments">
        {items.map((m) => (
          <li key={m.media_id}>
            <Thumbnail src={`/api/faces/${m.best_face_id}/thumbnail`} alt="" icon="video" />
            <div>
              <strong>{m.name}</strong>
              <div className="video-chips">
                {m.segments.map((s) => (
                  <button key={s.start} className="badge" onClick={() => setOpen({ id: m.media_id, t: s.start })}
                    aria-label={t("video.seek", { time: timeLabel(s.start) })}>{timeLabel(s.start)}–{timeLabel(s.end)}</button>
                ))}
              </div>
            </div>
          </li>
        ))}
      </ul>
      {open && <MediaViewer id={open.id} timestamp={open.t} onClose={() => setOpen(null)} />}
    </section>
  );
}
