import { useState } from "react";
import { bytes, mutate, number } from "../api";
import { useAction, useApp } from "../context";
import { refreshData, useResource } from "../hooks";
import { useT } from "../i18n";
import type { Job } from "../types";
import { Icon } from "./Icon";
import { Dialog, ErrorNotice } from "./ui";

export type PackageScope = { type: "library" } | { type: "person"; person_id: number } | { type: "selection"; media_ids: number[] };

/** Passphrase (twice) + options, then an encrypted export job. The passphrase is never stored. */
export function PackageExportDialog({ scope, onClose }: { scope: PackageScope; onClose: () => void }) {
  const t = useT();
  const action = useAction();
  const { jobs } = useApp();
  const [pass, setPass] = useState("");
  const [again, setAgain] = useState("");
  const [media, setMedia] = useState(scope.type !== "library");
  const [jobId, setJobId] = useState<number | null>(null);
  const job = jobId === null ? undefined : jobs.find((j) => j.id === jobId);
  const result = (job?.progress ?? {}) as { name?: string; bytes?: number };
  const problem = pass.length > 0 && pass.length < 8 ? t("pack.tooShort") : again && pass !== again ? t("pack.mismatch") : "";
  return (
    <Dialog open onClose={onClose} title={t("pack.exportTitle")} busy={action.busy}>
      <form onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          const created = await mutate<Job>("/packages/export", { scope, passphrase: pass, include_media: media });
          setJobId(created.id);
          setPass(""); setAgain("");
        }, undefined, false);
      }}>
        <div className="dialog-body share">
          <p className="small-text muted">{t(`pack.scope.${scope.type}`)} {t("pack.what")}</p>
          <label className="field">{t("pack.passphrase")}
            <input type="password" autoComplete="new-password" value={pass} onChange={(e) => setPass(e.target.value)} required minLength={8} />
          </label>
          <label className="field">{t("pack.passphraseAgain")}
            <input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} required />
          </label>
          {problem && <p className="small-text danger-text" role="alert">{problem}</p>}
          <label className="check-label">
            <input type="checkbox" checked={media} onChange={(e) => setMedia(e.target.checked)} />{t("pack.includeMedia")}
          </label>
          <p className="small-text muted">{t("pack.warning")}</p>
          {job && (
            <p role="status" className="small-text">
              {job.status === "completed" ? t("pack.exported", { name: result.name ?? "", size: bytes(result.bytes ?? 0) })
                : job.status === "failed" ? (job.error ?? "") : t("pack.working", { done: number(job.processed), total: number(job.total) })}
            </p>
          )}
          <ErrorNotice error={action.error} />
        </div>
        <div className="dialog-footer">
          <button type="button" className="button" onClick={onClose}>{t("common.close")}</button>
          {job?.status === "completed" && result.name
            ? <a className="button primary" href={`/api/packages/files/${result.name}`} download>{t("pack.download")}</a>
            : <button className="button primary" disabled={action.busy || pass.length < 8 || pass !== again || (!!job && job.status !== "failed")}>{t("pack.export")}</button>}
        </div>
      </form>
    </Dialog>
  );
}

interface PackageList { items: { name: string; bytes: number; created_at: string }[]; jobs: Job[] }

/** Health page section: export the whole library, list packages, import one. */
export function PackagesPanel() {
  const t = useT();
  const list = useResource<PackageList>("/packages");
  const action = useAction();
  const { jobs } = useApp();
  const [exporting, setExporting] = useState(false);
  const [source, setSource] = useState("");
  const [pass, setPass] = useState("");
  const [conflict, setConflict] = useState<"keep_both" | "skip" | "overwrite">("keep_both");
  const [importId, setImportId] = useState<number | null>(null);
  const importJob = importId === null ? undefined : jobs.find((j) => j.id === importId);
  const report = (importJob?.progress ?? {}) as { media_new?: number; media_matched?: number; people_new?: number; conflicts?: unknown[] };
  return (
    <section className="settings-section" aria-labelledby="packages-heading">
      <div className="section-heading"><div><h2 id="packages-heading">{t("pack.title")}</h2><p>{t("pack.help")}</p></div></div>
      <button className="button small" onClick={() => setExporting(true)}><Icon name="shield" size={16} />{t("pack.exportLibrary")}</button>
      {!!list.data?.items.length && (
        <ul className="health-backups">
          {list.data.items.map((item) => (
            <li key={item.name}>
              <span>{item.name} · {bytes(item.bytes)}</span>
              <a className="text-link" href={`/api/packages/files/${item.name}`} download>{t("pack.download")}</a>
              <button className="text-link" onClick={() => setSource(item.name)}>{t("pack.useForImport")}</button>
            </li>
          ))}
        </ul>
      )}
      <form className="package-import" onSubmit={(e) => {
        e.preventDefault();
        void action.run(async () => {
          const created = await mutate<Job>("/packages/import", { source: source.trim(), passphrase: pass, conflict });
          setImportId(created.id);
          setPass("");
        }, undefined, false);
      }}>
        <h3 className="field-label">{t("pack.importTitle")}</h3>
        <label className="field">{t("pack.source")}
          <input value={source} onChange={(e) => setSource(e.target.value)} spellCheck={false} required />
        </label>
        <label className="field">{t("pack.passphrase")}
          <input type="password" autoComplete="off" value={pass} onChange={(e) => setPass(e.target.value)} required />
        </label>
        <label className="field">{t("pack.conflict")}
          <select value={conflict} onChange={(e) => setConflict(e.target.value as typeof conflict)}>
            <option value="keep_both">{t("pack.conflict.keep_both")}</option>
            <option value="skip">{t("pack.conflict.skip")}</option>
            <option value="overwrite">{t("pack.conflict.overwrite")}</option>
          </select>
        </label>
        <button className="button small" disabled={action.busy || !source.trim() || !pass}>{t("pack.import")}</button>
        {importJob && (
          <p role="status" className="small-text">
            {importJob.status === "completed"
              ? t("pack.imported", { fresh: number(report.media_new ?? 0), matched: number(report.media_matched ?? 0), conflicts: number(report.conflicts?.length ?? 0) })
              : importJob.status === "failed" ? (importJob.error ?? "") : t("pack.verifying")}
          </p>
        )}
        <ErrorNotice error={action.error} />
      </form>
      {exporting && <PackageExportDialog scope={{ type: "library" }} onClose={() => { setExporting(false); refreshData(); }} />}
    </section>
  );
}
