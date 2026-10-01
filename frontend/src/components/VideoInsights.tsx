import { useEffect, useState, type RefObject } from "react";
import { exportZip, timeLabel } from "../api";
import { useAction } from "../context";
import { useResource } from "../hooks";
import { useT } from "../i18n";
import { Thumbnail } from "./ui";

interface Segment { start: number; end: number }
export interface VideoDetail {
  media_id: number;
  tracks: { track_id: number | null; person_id: number | null; display_name: string | null; start: number; end: number; best_face_id: number }[];
  people: { person_id: number; display_name: string; best_face_id: number; segments: Segment[] }[];
  keyframes: { id: number; t: number }[];
  analyzed: boolean;
}

/** Person lanes over the timeline (face markers), timestamp chips and per-person clip export. */
export function VideoInsights({ mediaId, duration, video, onSeek }: {
  mediaId: number; duration: number | null; video: RefObject<HTMLVideoElement | null>; onSeek: (t: number) => void;
}) {
  const t = useT();
  const detail = useResource<VideoDetail>(`/media/${mediaId}/video`);
  const action = useAction();
  const [now, setNow] = useState(0);
  useEffect(() => {
    // Media events do not bubble, but they can be captured at the document; this keeps working
    // when the viewer re-creates its <video> element (e.g. after a conversion).
    const update = (event: Event) => {
      if (event.target === video.current) setNow((event.target as HTMLVideoElement).currentTime);
    };
    document.addEventListener("timeupdate", update, true);
    document.addEventListener("seeked", update, true);
    return () => {
      document.removeEventListener("timeupdate", update, true);
      document.removeEventListener("seeked", update, true);
    };
  }, [video]);
  const data = detail.data;
  if (!data || (!data.people.length && !data.keyframes.length)) return null;
  const total = Math.max(duration ?? 0, ...data.keyframes.map((k) => k.t), ...data.people.flatMap((p) => p.segments.map((s) => s.end)), 1);
  const pct = (v: number) => `${Math.min(100, (v / total) * 100)}%`;
  return (
    <section className="metadata-section video-insights" aria-labelledby={`video-insights-${mediaId}`}>
      <h3 id={`video-insights-${mediaId}`}>{t("video.whoAndWhen")}</h3>
      {data.people.map((person) => (
        <div key={person.person_id} className="video-lane">
          <div className="video-lane-head">
            <Thumbnail src={`/api/faces/${person.best_face_id}/thumbnail`} alt="" icon="people" />
            <strong>{person.display_name}</strong>
            <button className="text-link small-text" disabled={action.busy}
              onClick={() => void action.run(() => exportZip(`/media/${mediaId}/person-clips`, { person_id: person.person_id }, "clips.zip"),
                t("video.clipsSaved"), false)}>
              {t("video.exportClips")}
            </button>
          </div>
          <div className="video-track" role="group" aria-label={t("video.lane", { name: person.display_name })}>
            {person.segments.map((s) => (
              <button key={s.start} className="video-segment" style={{ left: pct(s.start), width: `max(6px, ${pct(s.end - s.start)})` }}
                onClick={() => onSeek(s.start)} aria-label={t("video.seek", { time: timeLabel(s.start) })} title={`${timeLabel(s.start)} – ${timeLabel(s.end)}`} />
            ))}
            <span className="video-playhead" style={{ left: pct(now) }} aria-hidden="true" />
          </div>
          <div className="video-chips">
            {person.segments.map((s) => (
              <button key={s.start} className="badge" onClick={() => onSeek(s.start)}
                aria-label={t("video.seekPerson", { name: person.display_name, time: timeLabel(s.start) })}>
                {timeLabel(s.start)}
              </button>
            ))}
          </div>
        </div>
      ))}
      {data.keyframes.length > 0 && (
        <>
          <h4 className="field-label">{t("video.scenes")}</h4>
          <div className="video-keyframes">
            {data.keyframes.map((k) => (
              <button key={k.id} onClick={() => onSeek(k.t)} aria-label={t("video.seek", { time: timeLabel(k.t) })}
                className={now >= k.t ? "passed" : ""}>
                <img src={`/api/keyframes/${k.id}/image`} alt="" loading="lazy" />
                <span>{timeLabel(k.t)}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
