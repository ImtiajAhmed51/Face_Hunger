import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { mutate, number, request } from "../api";
import { useAction, useApp } from "../context";
import { useDebounced } from "../hooks";
import { useT } from "../i18n";
import type { Job } from "../types";
import { Dialog, ErrorNotice, Thumbnail } from "./ui";

interface SharePeople {
  people: { id: number; display_name: string; media_count: number; face_id: number }[];
  unknown_faces: number; photos: number; videos: number;
}
type Method = "blur" | "pixelate" | "mask";

/** Choose who stays visible, how the rest is hidden, preview it, then export new files. */
export function ShareDialog({ ids, onClose }: { ids: number[]; onClose: () => void }) {
  const t = useT();
  const { jobs } = useApp();
  const action = useAction();
  const [keep, setKeep] = useState<Set<number>>(new Set());
  const [method, setMethod] = useState<Method>("blur");
  const [strength, setStrength] = useState(0.7);
  const [strip, setStrip] = useState(true);
  const [folder, setFolder] = useState("");
  const [toFolder, setToFolder] = useState(false);
  const [jobId, setJobId] = useState<number | null>(null);
  const [after, setAfter] = useState<string | null>(null);
  const info = useQuery({
    queryKey: ["share-people", ids],
    queryFn: ({ signal }) => request<SharePeople>("/share/people", { method: "POST", body: { media_ids: ids }, signal }),
  });
  const options = useMemo(() => ({
    mode: "all_except_kept", keep_people: [...keep], method, strength, strip_metadata: strip, strip_gps: true,
    destination: toFolder ? { type: "folder", path: folder.trim() } : { type: "zip" },
  }), [keep, method, strength, strip, toFolder, folder]);
  const previewKey = useDebounced(JSON.stringify({ keep: [...keep], method, strength }), 350);
  const sample = ids[0];

  useEffect(() => {
    let url: string | null = null;
    const controller = new AbortController();
    void fetch("/api/share/preview", {
      method: "POST", signal: controller.signal, headers: { "Content-Type": "application/json", "X-LFS-Request": "1" },
      body: JSON.stringify({ media_id: sample, options: { ...JSON.parse(previewKey), keep_people: JSON.parse(previewKey).keep } }),
    }).then((r) => (r.ok ? r.blob() : null)).then((blob) => {
      if (!blob) { setAfter(null); return; }
      url = URL.createObjectURL(blob);
      setAfter(url);
    }).catch(() => {});
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [sample, previewKey]);

  const job: Job | undefined = jobId === null ? undefined : jobs.find((j) => j.id === jobId);
  const progress = (job?.progress ?? {}) as { exported?: number; skipped?: Record<string, string>; faces_anonymized?: number; destination?: string };
  const finished = job?.status === "completed";
  const skipped = Object.keys(progress.skipped ?? {}).length;

  return (
    <Dialog open onClose={onClose} title={t("share.title")} className="share-dialog" busy={action.busy}>
      <div className="dialog-body share">
        <p className="small-text muted">{t("share.intro", { photos: number(info.data?.photos ?? 0) })}
          {info.data?.videos ? ` ${t("share.videosSkipped", { count: number(info.data.videos) })}` : ""}</p>
        <fieldset className="share-people">
          <legend>{t("share.who")}</legend>
          {(info.data?.people ?? []).map((p) => (
            <label key={p.id} className="check-label">
              <input type="checkbox" checked={keep.has(p.id)} onChange={(e) => setKeep((prev) => {
                const next = new Set(prev);
                if (e.target.checked) next.add(p.id); else next.delete(p.id);
                return next;
              })} />
              <Thumbnail src={`/api/faces/${p.face_id}/thumbnail`} alt="" icon="people" />
              {t("share.keepVisible", { name: p.display_name })}
            </label>
          ))}
          <p className="small-text muted">{t("share.everyoneElse", { count: number(info.data?.unknown_faces ?? 0) })}</p>
        </fieldset>
        <div className="edit-row" role="radiogroup" aria-label={t("share.method")}>
          {(["blur", "pixelate", "mask"] as const).map((m) => (
            <button key={m} role="radio" aria-checked={method === m} className="button small" onClick={() => setMethod(m)}>{t(`share.method.${m}`)}</button>
          ))}
          <label className="field-inline">{t("share.strength")}
            <input type="range" min={0} max={1} step={0.05} value={strength} disabled={method === "mask"}
              onChange={(e) => setStrength(Number(e.target.value))} />
          </label>
        </div>
        <div className="share-compare">
          <figure><img src={`/api/media/${sample}/preview`} alt={t("share.before")} /><figcaption>{t("share.before")}</figcaption></figure>
          <figure>{after ? <img src={after} alt={t("share.after")} /> : <div className="cell-skeleton" aria-hidden="true" />}<figcaption>{t("share.after")}</figcaption></figure>
        </div>
        <label className="check-label">
          <input type="checkbox" checked={strip} onChange={(e) => setStrip(e.target.checked)} />{t("share.strip")}
        </label>
        <label className="check-label">
          <input type="checkbox" checked={toFolder} onChange={(e) => setToFolder(e.target.checked)} />{t("share.toFolder")}
        </label>
        {toFolder && (
          <label className="field">{t("share.folder")}
            <input value={folder} onChange={(e) => setFolder(e.target.value)} spellCheck={false} />
          </label>
        )}
        {job && (
          <p role="status" className="small-text">
            {finished ? t("share.done", { count: number(progress.exported ?? 0), faces: number(progress.faces_anonymized ?? 0) })
              : job.status === "failed" ? (job.error ?? "") : t("share.working", { done: number(job.processed), total: number(job.total) })}
            {finished && skipped > 0 ? ` ${t("share.skipped", { count: number(skipped) })}` : ""}
            {finished && toFolder && progress.destination ? ` ${progress.destination}` : ""}
          </p>
        )}
        <ErrorNotice error={action.error} />
        <p className="small-text muted">{t("share.safe")}</p>
      </div>
      <div className="dialog-footer">
        <button className="button" onClick={onClose}>{t("common.close")}</button>
        {job && !finished && job.status !== "failed" && job.status !== "cancelled" && (
          <button className="button" onClick={() => void mutate(`/jobs/${job.id}/cancel`)}>{t("edit.cancel")}</button>
        )}
        {finished && !toFolder
          ? <a className="button primary" href={`/api/share/${job!.id}/download`} download>{t("share.download")}</a>
          : <button className="button primary" disabled={action.busy || (toFolder && !folder.trim()) || (!!job && !finished && job.status !== "failed")}
              onClick={() => void action.run(async () => {
                const created = await mutate<Job>("/share", { media_ids: ids, options });
                setJobId(created.id);
              }, undefined, false)}>{t("share.start", { count: number(ids.length) })}</button>}
      </div>
    </Dialog>
  );
}
