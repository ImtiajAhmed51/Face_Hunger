import { useResource } from "../hooks";
import { useT, type MessageKey } from "../i18n";
import type { QualityReport } from "../types";

const SIGNALS = ["sharpness", "exposure", "noise", "face_quality", "eyes_open", "smile", "aesthetic"] as const;

/** Best-shot score and its per-signal breakdown for the viewer's info panel. */
export function QualityPanel({ mediaId }: { mediaId: number }) {
  const t = useT();
  const report = useResource<QualityReport>(`/media/${mediaId}/quality`);
  const data = report.data;
  if (!data) return null;
  return (
    <section className="metadata-section" aria-labelledby={`quality-${mediaId}`}>
      <h3 id={`quality-${mediaId}`}>{t("quality.title")}</h3>
      {!data.signals ? (
        <p className="small-text muted">{t("quality.pending")}</p>
      ) : data.error ? (
        <p className="small-text muted">{t("quality.failed")}</p>
      ) : (
        <>
          <p className="quality-score">
            <strong>{data.score === null ? "–" : Math.round(data.score * 100)}</strong>
            <span className="muted small-text"> / 100 · {t("quality.formula", { version: data.formula_version })}</span>
          </p>
          <ul className="health-bars quality-bars">
            {SIGNALS.map((name) => {
              const value = data.signals?.[name];
              return (
                <li key={name}>
                  <span>{t(`quality.${name}` as MessageKey)}</span>
                  {value === null || value === undefined ? (
                    <span className="muted small-text">{t("quality.notApplicable")}</span>
                  ) : (
                    <meter min={0} max={1} low={0.4} high={0.7} optimum={1} value={value}
                      aria-label={`${t(`quality.${name}` as MessageKey)} ${Math.round(value * 100)}`} />
                  )}
                  <span className="small-text muted">
                    {value === null || value === undefined ? "" : `${Math.round(value * 100)} · ×${data.weights[name]}`}
                  </span>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
