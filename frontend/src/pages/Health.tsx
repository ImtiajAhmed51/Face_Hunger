import { useState } from "react";
import { bytes, mutate, number } from "../api";
import { useAction, useApp } from "../context";
import { refreshData, useResource } from "../hooks";
import type { Job } from "../types";
import { Icon } from "../components/Icon";
import { ActivityLog } from "../components/Memories";
import { PackagesPanel } from "../components/Packages";
import { ModelUpgrade } from "./Storage";
import { Badge, ConfirmDialog, Empty, ErrorNotice, Loading, PageHeader } from "../components/ui";

interface Problem { severity: "error" | "warning" | "info"; kind: string; message: string; count: number; sample: (number | string)[] }
interface HealthSummary {
  status: "ok" | "degraded" | "down";
  degraded: string[];
  uptime_s: number;
  checks: Record<string, { ok: boolean; [key: string]: unknown }>;
  config_warnings: string[];
}
interface Usage { bytes: number; files: number }
interface LibraryHealth {
  counts: Record<string, number>;
  storage: Record<string, Usage> & { total_bytes: number; disk: { total: number; free: number } };
  integrity: { ok: boolean; finished_at: string; problems: Problem[]; files: Record<string, number> } | null;
  embeddings: { key: string; role: string; coverage: { filled: number; total: number; ratio: number }; index_size: number | null }[];
  restored: { restored_files: number; previous_data: string } | null;
}
interface BackupList { items: { name: string; bytes: number; created_at: string }[] }

const STORAGE_LABELS: Record<string, string> = {
  database: "Database", face_embeddings: "Face embeddings", media_embeddings: "Legacy visual embeddings",
  vector_stores: "Embedding stores", thumbnails: "Thumbnails", video_previews: "Video previews", backups: "Backups & exports",
};
const TONE = { error: "red", warning: "amber", info: "" } as const;

function useJob(kind: string): Job | undefined {
  const { jobs } = useApp();
  return jobs.find((job) => job.kind === kind && ["queued", "running", "paused"].includes(job.status));
}

function progressText(job?: Job): string {
  if (!job) return "";
  const p = job.progress ?? {};
  const phase = (p.phase as string) || job.phase;
  const pct = job.total ? ` · ${Math.round((job.processed / job.total) * 100)}%` : "";
  return `${phase}${pct}`;
}

export function Health() {
  const summary = useResource<HealthSummary>("/health");
  const library = useResource<LibraryHealth>("/health/library");
  const backups = useResource<BackupList>("/backup");
  const action = useAction();
  const checking = useJob("integrity_check");
  const exporting = useJob("backup_export");
  const restoring = useJob("backup_restore");
  const [restore, setRestore] = useState<string | null>(null);
  const data = library.data;
  const storageMax = data ? Math.max(1, ...Object.keys(STORAGE_LABELS).map((k) => data.storage[k]?.bytes ?? 0)) : 1;

  return (
    <>
      <PageHeader
        eyebrow="KEEP IT HEALTHY"
        title="Library health"
        description="Missing files, index integrity, storage and backups. Checks only report problems; nothing is changed or deleted."
        actions={
          <button className="button primary" disabled={!!checking || action.busy}
            onClick={() => void action.run(() => mutate("/health/check", {}), "Integrity check started.")}>
            <Icon name="shield" size={16} />
            {checking ? `Checking… ${progressText(checking)}` : "Run integrity check"}
          </button>
        }
      />
      <ErrorNotice error={summary.error || library.error} retry={() => { summary.reload(); library.reload(); }} />
      {summary.loading || library.loading ? <Loading label="Checking your library" /> : null}
      {summary.data && (
        <section className="settings-section" aria-labelledby="status-heading">
          <div className="section-heading">
            <div>
              <h2 id="status-heading">Status</h2>
              <p>Up for {Math.round(summary.data.uptime_s / 60)} min</p>
            </div>
            <Badge tone={summary.data.status === "ok" ? "teal" : summary.data.status === "down" ? "red" : "amber"}>
              {summary.data.status === "ok" ? "All systems normal" : `Degraded: ${summary.data.degraded.join(", ")}`}
            </Badge>
          </div>
          <dl className="metadata">
            {Object.entries(summary.data.checks).map(([name, check]) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd>{check.ok ? "OK" : "Needs attention"}{name === "disk" ? ` · ${bytes(Number(check.free_bytes))} free` : ""}</dd>
              </div>
            ))}
          </dl>
          {summary.data.config_warnings.map((warning) => (
            <p key={warning} className="search-unmatched" role="note"><Icon name="alert" size={16} /> {warning}</p>
          ))}
        </section>
      )}
      {data?.restored && (
        <p className="search-unmatched" role="status">
          <Icon name="restore" size={16} /> A backup was restored at startup ({data.restored.restored_files} files).
          Your previous data is kept in {data.restored.previous_data}.
        </p>
      )}
      {data && (
        <div className="settings-layout">
          <div className="settings-primary">
            <section className="settings-section" aria-labelledby="integrity-heading">
              <div className="section-heading">
                <div>
                  <h2 id="integrity-heading">Integrity</h2>
                  <p>{data.integrity ? `Last checked ${new Date(data.integrity.finished_at).toLocaleString()}` : "Not checked yet"}</p>
                </div>
                {data.integrity && <Badge tone={data.integrity.ok ? "teal" : "red"}>{data.integrity.ok ? "No errors" : "Errors found"}</Badge>}
              </div>
              {!data.integrity ? (
                <Empty icon="shield" title="No integrity report yet"
                  description="Run a check to verify originals on disk, embedding checksums, vector indexes and database structure." />
              ) : data.integrity.problems.length === 0 ? (
                <p className="muted">Everything checked out: {number(data.integrity.files.checked)} originals, every embedding store and index.</p>
              ) : (
                <ul className="health-problems" aria-label="Problems found">
                  {data.integrity.problems.map((problem) => (
                    <li key={problem.kind}>
                      <Badge tone={TONE[problem.severity]}>{problem.severity}</Badge>
                      <span>{problem.message}</span>
                      {problem.sample.length > 0 && <span className="muted small-text">e.g. {problem.sample.slice(0, 5).join(", ")}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <ModelUpgrade />
            <PackagesPanel />
            <ActivityLog />
            <section className="settings-section" aria-labelledby="embeddings-heading">
              <div className="section-heading">
                <div><h2 id="embeddings-heading">Embeddings</h2><p>Coverage of each model's store</p></div>
                <button className="button small" disabled={action.busy}
                  onClick={() => void action.run(() => mutate("/jobs", { kind: "embed_backfill", priority: 80 }), "Embedding job queued.")}>
                  <Icon name="spark" size={16} />Compute missing
                </button>
              </div>
              <ul className="health-bars">
                {data.embeddings.map((space) => (
                  <li key={space.key}>
                    <span>{space.key}<span className="muted small-text"> · {space.role}</span></span>
                    <progress max={Math.max(1, space.coverage.total)} value={space.coverage.filled}
                      aria-label={`${space.key} coverage`} />
                    <span className="small-text">{number(space.coverage.filled)} / {number(space.coverage.total)}</span>
                  </li>
                ))}
              </ul>
            </section>
          </div>
          <aside className="settings-secondary">
            <section className="settings-section" aria-labelledby="storage-heading">
              <div className="section-heading"><div><h2 id="storage-heading">Storage</h2>
                <p>{bytes(data.storage.total_bytes)} used by Face Hunger · {bytes(data.storage.disk.free)} free on disk</p></div></div>
              <ul className="health-bars">
                {Object.entries(STORAGE_LABELS).map(([key, label]) => (
                  <li key={key}>
                    <span>{label}</span>
                    <meter min={0} max={storageMax} value={data.storage[key]?.bytes ?? 0} aria-label={`${label} size`} />
                    <span className="small-text">{bytes(data.storage[key]?.bytes ?? 0)}</span>
                  </li>
                ))}
              </ul>
              <dl className="metadata">
                <div><dt>Media</dt><dd>{number(data.counts.media)}</dd></div>
                <div><dt>Missing</dt><dd>{number(data.counts.missing)}</dd></div>
                <div><dt>Failed</dt><dd>{number(data.counts.failed)}</dd></div>
                <div><dt>People</dt><dd>{number(data.counts.people)}</dd></div>
                <div><dt>Originals</dt><dd>{bytes(data.counts.original_bytes)}</dd></div>
              </dl>
            </section>
            <section className="settings-section" aria-labelledby="backup-heading">
              <div className="section-heading"><div><h2 id="backup-heading">Backups</h2>
                <p>Index, embeddings and people. Originals are never included.</p></div></div>
              <button className="button small" disabled={!!exporting || action.busy}
                onClick={() => void action.run(() => mutate("/backup/export", {}), "Backup started.")}>
                <Icon name="download" size={16} />
                {exporting ? `Backing up… ${progressText(exporting)}` : "Create backup"}
              </button>
              {restoring && <p className="small-text muted" role="status">Verifying backup… {progressText(restoring)}</p>}
              {!backups.data?.items.length ? (
                <p className="muted small-text">No backups yet. They are written to your data folder under exports/.</p>
              ) : (
                <ul className="health-backups">
                  {backups.data.items.map((item) => (
                    <li key={item.name}>
                      <span>{new Date(item.created_at).toLocaleString()} · {bytes(item.bytes)}</span>
                      <a className="text-link" href={`/api/backup/files/${item.name}`} download>Download</a>
                      <button className="text-link" onClick={() => setRestore(item.name)}>Restore…</button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </aside>
        </div>
      )}
      <ConfirmDialog
        open={restore !== null}
        onClose={() => setRestore(null)}
        title="Restore this backup?"
        description="The backup is verified and applied the next time Face Hunger starts. Your current index is moved aside to data/backups/, not deleted. Type RESTORE to continue."
        label="Restore on restart"
        confirmation="RESTORE"
        onConfirm={async () => { if (restore) await mutate("/backup/restore", { name: restore }); refreshData(); }}
        success="Backup verified in the background. Restart Face Hunger to apply it."
      />
    </>
  );
}
