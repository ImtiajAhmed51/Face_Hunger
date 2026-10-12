import { Button } from "@/components/primitives/button";
import { useState } from "react";
import { bytes, mutate, number } from "../api";
import { useAction, useApp } from "../context";
import { useResource } from "../hooks";
import { useT } from "../i18n";
import type { Job } from "../types";
import { ErrorNotice } from "./ui";

interface PackageList { items: { name: string; bytes: number; created_at: string }[]; jobs: Job[] }

export function PackagesPanel() {
  const t = useT();
  const list = useResource<PackageList>("/packages");
  const action = useAction();
  const { jobs } = useApp();
  const [source, setSource] = useState("");
  const [pass, setPass] = useState("");
  const [conflict, setConflict] = useState<"keep_both" | "skip" | "overwrite">("keep_both");
  const [importId, setImportId] = useState<number | null>(null);
  const importJob = importId === null ? undefined : jobs.find((j) => j.id === importId);
  const report = (importJob?.progress ?? {}) as { media_new?: number; media_matched?: number; people_new?: number; conflicts?: unknown[] };
  return (
    <section className="settings-section" aria-labelledby="packages-heading">
      <div className="section-heading"><div><h2 id="packages-heading">{t("pack.title")}</h2><p>{t("pack.help")}</p></div></div>
      {!!list.data?.items.length && (
        <ul className="health-backups">
          {list.data.items.map((item) => (
            <li key={item.name}>
              <span>{item.name} · {bytes(item.bytes)}</span>
              <Button className="text-link" onClick={() => setSource(item.name)}>{t("pack.useForImport")}</Button>
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
        <Button className="button small" disabled={action.busy || !source.trim() || !pass}>{t("pack.import")}</Button>
        {importJob && (
          <p role="status" className="small-text">
            {importJob.status === "completed"
              ? t("pack.imported", { fresh: number(report.media_new ?? 0), matched: number(report.media_matched ?? 0), conflicts: number(report.conflicts?.length ?? 0) })
              : importJob.status === "failed" ? (importJob.error ?? "") : t("pack.verifying")}
          </p>
        )}
        <ErrorNotice error={action.error} />
      </form>
    </section>
  );
}
