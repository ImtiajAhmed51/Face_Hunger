import { bytes, mutate, number } from "../api";
import { useAction } from "../context";
import { setDiagnostics } from "../diag";
import { useResource } from "../hooks";
import { useT } from "../i18n";
import { ErrorNotice, Loading, PageHeader } from "../components/ui";

interface Operation { kind: string; name: string; count: number; p50_ms: number; p95_ms: number; max_ms: number }
interface Summary {
  enabled: boolean; events: number; bytes: number; since: string | null;
  operations: Operation[];
  slow: { at: string; kind: string; name: string; ms: number }[];
  model_loads: { at: string; name: string; ms: number }[];
  caches: { name: string; hits: number; misses: number; hit_rate: number }[];
}

const ms = (value: number) => value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;

export function Diagnostics() {
  const t = useT();
  const data = useResource<Summary>("/diagnostics");
  const action = useAction();
  const summary = data.data;
  const call = (fn: () => Promise<unknown>, done?: string) => void action.run(async () => { await fn(); data.reload(); }, done, false);
  return (
    <>
      <PageHeader eyebrow={t("diag.eyebrow")} title={t("diag.title")} description={t("diag.description")} />
      <ErrorNotice error={data.error || action.error} retry={data.reload} />
      {data.loading || !summary ? <Loading label={t("common.loading")} /> : (
        <>
          <section className="settings-section" aria-labelledby="diag-switch">
            <h2 id="diag-switch" className="sr-only">{t("diag.switch")}</h2>
            <label className="check-label">
              <input type="checkbox" checked={summary.enabled} disabled={action.busy} onChange={(e) => {
                const enabled = e.target.checked;
                call(async () => { await mutate("/diagnostics", { enabled }, "PATCH"); setDiagnostics(enabled); });
              }} />
              {t("diag.switch")}
            </label>
            <p className="small-text muted" role="status">
              {summary.enabled ? t("diag.on") : t("diag.off")} {t("diag.stored", { count: number(summary.events), size: bytes(summary.bytes) })}
            </p>
            <div className="inline-actions">
              <a className="button small" href="/api/diagnostics/report" download>{t("diag.export")}</a>
              <button className="button small" disabled={action.busy} onClick={() => data.reload()}>{t("diag.refresh")}</button>
              <button className="button small danger-text" disabled={action.busy || !summary.events}
                onClick={() => call(() => mutate("/diagnostics", undefined, "DELETE"), t("diag.cleared"))}>{t("diag.clear")}</button>
            </div>
            <p className="small-text muted">{t("diag.exportHelp")}</p>
          </section>
          {summary.events === 0 ? <p className="muted">{t("diag.empty")}</p> : (
            <>
              <section className="settings-section" aria-labelledby="diag-ops">
                <h2 id="diag-ops">{t("diag.operations")}</h2>
                <div className="table-scroll">
                  <table className="diag-table">
                    <thead><tr><th scope="col">{t("diag.operation")}</th><th scope="col">{t("diag.count")}</th>
                      <th scope="col">{t("diag.p50")}</th><th scope="col">{t("diag.p95")}</th><th scope="col">{t("diag.max")}</th></tr></thead>
                    <tbody>{summary.operations.slice(0, 40).map((o) => (
                      <tr key={`${o.kind}:${o.name}`}><th scope="row">{o.kind} · {o.name}</th><td>{number(o.count)}</td>
                        <td>{ms(o.p50_ms)}</td><td>{ms(o.p95_ms)}</td><td>{ms(o.max_ms)}</td></tr>
                    ))}</tbody>
                  </table>
                </div>
              </section>
              <section className="settings-section" aria-labelledby="diag-slow">
                <h2 id="diag-slow">{t("diag.slow")}</h2>
                <ol className="diag-list">{summary.slow.slice(0, 12).map((e, i) => (
                  <li key={i}><span>{e.kind} · {e.name}</span><strong>{ms(e.ms)}</strong></li>
                ))}</ol>
              </section>
              <section className="settings-section" aria-labelledby="diag-models">
                <h2 id="diag-models">{t("diag.modelLoads")}</h2>
                {summary.model_loads.length === 0 ? <p className="muted small-text">{t("diag.noModelLoads")}</p> : (
                  <ul className="diag-list">{summary.model_loads.slice(0, 12).map((e, i) => (
                    <li key={i}><span>{e.name}</span><strong>{ms(e.ms)}</strong></li>
                  ))}</ul>
                )}
              </section>
              <section className="settings-section" aria-labelledby="diag-caches">
                <h2 id="diag-caches">{t("diag.caches")}</h2>
                {summary.caches.length === 0 ? <p className="muted small-text">{t("diag.noCaches")}</p> : (
                  <ul className="diag-list">{summary.caches.map((c) => (
                    <li key={c.name}><span>{c.name}</span>
                      <strong>{t("diag.hitRate", { rate: Math.round(c.hit_rate * 100), hits: number(c.hits), total: number(c.hits + c.misses) })}</strong></li>
                  ))}</ul>
                )}
              </section>
            </>
          )}
        </>
      )}
    </>
  );
}
