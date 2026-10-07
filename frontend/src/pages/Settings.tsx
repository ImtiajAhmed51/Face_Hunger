import { useEffect, useState } from "react";
import { AppLockCard } from "../components/AppLock";
import { Link } from "react-router-dom";
import { bytes, mutate } from "../api";
import { useAction, useApp } from "../context";
import { useResource } from "../hooks";
import type {
  Engine,
  Exclusion,
  Settings as SettingsData,
  Theme,
} from "../types";
import { Icon } from "../components/Icon";
import { JobCard } from "../components/JobCard";
import { Libraries } from "../components/Libraries";
import { MediaViewer } from "../components/MediaViewer";
import {
  Badge,
  ConfirmDialog,
  ErrorNotice,
  Loading,
  PageHeader,
} from "../components/ui";
import { EngineStatus } from "./Home";
import { LOCALES, getLocale, setLocale, useT, type Locale } from "../i18n";

type AutotuneReport = {
  threshold: number;
  suggested: number;
  f_beta: number;
  precision: number;
  recall: number;
  n_samples: number;
};

function Preferences({ settings }: { settings: SettingsData }) {
  const [draft, setDraft] = useState({
    matching_threshold: settings.matching_threshold,
    review_threshold: settings.review_threshold,
    detection_size: settings.detection_size,
    video_interval: settings.video_interval,
    theme: settings.theme,
    dino_similarity_threshold: settings.dino_similarity_threshold ?? 0.92,
  });
  const action = useAction();
  const autotune = useAction();
  const { setTheme, notify } = useApp();
  useEffect(
    () =>
      setDraft({
        matching_threshold: settings.matching_threshold,
        review_threshold: settings.review_threshold,
        detection_size: settings.detection_size,
        video_interval: settings.video_interval,
        theme: settings.theme,
        dino_similarity_threshold: settings.dino_similarity_threshold ?? 0.92,
      }),
    [settings],
  );
  const dirty = Object.entries(draft).some(
    ([key, value]) => settings[key as keyof typeof draft] !== value,
  );
  return (
    <form
      className="settings-section"
      onSubmit={async (event) => {
        event.preventDefault();
        if (
          await action.run(
            () => mutate("/settings", draft, "PATCH"),
            "Preferences saved.",
          )
        )
          setTheme(draft.theme);
      }}
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">FINE-TUNE YOUR COLLECTION</p>
          <h2>Recognition & appearance</h2>
        </div>
        <button
          className="button primary small"
          disabled={!dirty || action.busy}
        >
          {action.busy ? "Saving..." : "Save changes"}
        </button>
      </div>
      <div className="settings-fields">
        <div className="setting-row">
          <div>
            <label htmlFor="matching-threshold">Matching threshold</label>
            <p>
              Minimum similarity used for identity matching. A higher value is
              more conservative.
            </p>
          </div>
          <div className="range-control">
            <input
              id="matching-threshold"
              type="range"
              min="0.3"
              max="0.8"
              step="0.01"
              value={draft.matching_threshold}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  matching_threshold: Number(event.target.value),
                }))
              }
            />
            <output htmlFor="matching-threshold">
              {draft.matching_threshold.toFixed(2)}
            </output>
          </div>
          <button
            type="button"
            className="button small"
            disabled={autotune.busy}
            onClick={() =>
              void autotune.run(async () => {
                const report = await mutate<AutotuneReport>(
                  "/settings/autotune",
                  {},
                  "POST",
                );
                notify(
                  `Threshold tuned to ${report.threshold.toFixed(2)} from ${report.n_samples} reviewed faces (precision ${(report.precision * 100).toFixed(0)}%, recall ${(report.recall * 100).toFixed(0)}%).`,
                );
              })
            }
          >
            {autotune.busy ? "Tuning..." : "Auto-tune from reviews"}
          </button>
          <ErrorNotice error={autotune.error} />
        </div>
        <div className="setting-row">
          <div>
            <label htmlFor="review-threshold">Review threshold</label>
            <p>Matches below this confidence need a human check.</p>
          </div>
          <div className="range-control">
            <input
              id="review-threshold"
              type="range"
              min="0.4"
              max="0.95"
              step="0.01"
              value={draft.review_threshold}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  review_threshold: Number(event.target.value),
                }))
              }
            />
            <output htmlFor="review-threshold">
              {draft.review_threshold.toFixed(2)}
            </output>
          </div>
        </div>
        <div className="setting-row">
          <div>
            <label htmlFor="dino-threshold">Duplicate similarity</label>
            <p>
              DINOv2 cosine similarity for near-duplicate media. Higher is
              stricter (fewer groups). Default 0.92.
            </p>
          </div>
          <div className="range-control">
            <input
              id="dino-threshold"
              type="range"
              min="0.7"
              max="0.99"
              step="0.01"
              value={draft.dino_similarity_threshold}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  dino_similarity_threshold: Number(event.target.value),
                }))
              }
            />
            <output htmlFor="dino-threshold">
              {draft.dino_similarity_threshold.toFixed(2)}
            </output>
          </div>
        </div>
        <div className="setting-row">
          <div>
            <label htmlFor="detection-size">Detection size</label>
            <p>
              Larger images can find smaller faces, but take more processing
              time.
            </p>
          </div>
          <select
            id="detection-size"
            value={draft.detection_size}
            onChange={(event) =>
              setDraft((current) => ({
                ...current,
                detection_size: Number(event.target.value) as 320 | 640 | 960,
              }))
            }
          >
            <option value="320">320 px · faster</option>
            <option value="640">640 px · balanced</option>
            <option value="960">960 px · more detail</option>
          </select>
        </div>
        <div className="setting-row">
          <div>
            <label htmlFor="video-interval">Video sampling interval</label>
            <p>
              Seconds between sampled frames. A shorter interval finds more
              moments.
            </p>
          </div>
          <div className="suffix-input">
            <input
              id="video-interval"
              type="number"
              min="1"
              max="30"
              step="1"
              required
              value={draft.video_interval}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  video_interval: Number(event.target.value),
                }))
              }
            />
            <span>seconds</span>
          </div>
        </div>
        <fieldset className="theme-fieldset">
          <legend>Make yourself at home</legend>
          <p className="muted">
            Choose your preferred appearance. System follows your device.
          </p>
          <div className="theme-options">
            {(["light", "dark", "system"] as Theme[]).map((theme) => (
              <label
                className={`theme-option theme-${theme} ${draft.theme === theme ? "active" : ""}`}
                key={theme}
              >
                <span className="theme-preview">
                  <i />
                  <span>
                    <b />
                    <b />
                    <b />
                  </span>
                </span>
                <span>
                  <input
                    type="radio"
                    name="theme"
                    value={theme}
                    checked={draft.theme === theme}
                    onChange={() =>
                      setDraft((current) => ({ ...current, theme }))
                    }
                  />
                  {theme[0].toUpperCase() + theme.slice(1)}
                </span>
              </label>
            ))}
          </div>
        </fieldset>
      </div>
      <p className="small-text muted">
        Recognition settings apply to subsequent processing. Existing
        corrections are not changed by saving preferences.
      </p>
      <ErrorNotice error={action.error} />
    </form>
  );
}

function Exclusions() {
  const resource = useResource<{ items: Exclusion[] }>("/exclusions");
  const action = useAction();
  const [viewer, setViewer] = useState<number | null>(null);
  return (
    <section className="settings-section">
      <div className="section-heading">
        <div>
          <p className="eyebrow">YOUR CORRECTIONS, YOUR CONTROL</p>
          <h2>Excluded matches</h2>
        </div>
        <Badge>{resource.data?.items.length ?? 0}</Badge>
      </div>
      <p className="muted">
        Exclusions hide a person-media match from default results, without
        deleting faces or files. Search can include excluded matches.
      </p>
      <ErrorNotice error={resource.error} retry={resource.reload} />
      {resource.loading && !resource.data ? (
        <Loading label="Loading exclusions" />
      ) : resource.data && !resource.data.items.length ? (
        <p className="settings-empty">
          No excluded matches. Exclude a match from a face's actions or a
          person's media selection.
        </p>
      ) : (
        <div className="exclusions-list">
          {resource.data?.items.map((item) => (
            <div
              className="exclusion-row"
              key={`${item.person_id}:${item.media_id}`}
            >
              <div>
                <Link to={`/people/${item.person_id}`}>
                  {item.display_name}
                </Link>
                <button
                  className="text-link filename-link"
                  onClick={() => setViewer(item.media_id)}
                >
                  {item.name}
                </button>
              </div>
              <button
                className="button small"
                disabled={action.busy}
                onClick={() =>
                  void action.run(
                    () =>
                      mutate(
                        "/exclusions",
                        { person_id: item.person_id, media_id: item.media_id },
                        "DELETE",
                      ),
                    "Match included in default results again.",
                  )
                }
              >
                <Icon name="eye" size={15} />
                Include again
              </button>
            </div>
          ))}
        </div>
      )}
      {viewer !== null && (
        <MediaViewer id={viewer} onClose={() => setViewer(null)} />
      )}
    </section>
  );
}

const maintenance = {
  thumbnails: {
    title: "Clear thumbnails",
    confirm: "CLEAR THUMBNAILS",
    description:
      "Clear cached media and face thumbnails. Original files stay. After clearing, use Rebuild thumbnails (or open People/Photos) to regenerate previews.",
    label: "Clear thumbnails",
  },
  rebuild_thumbnails: {
    title: "Rebuild thumbnails",
    confirm: "REBUILD THUMBNAILS",
    description:
      "Regenerate person face tiles (photos preferred) and a batch of media previews from the original files. Safe; originals are not modified.",
    label: "Rebuild thumbnails",
  },
  index: {
    title: "Clear AI index",
    confirm: "CLEAR AI INDEX",
    description:
      "Remove the AI face index and its identity data. You will need to run indexing again. Original photos and videos are not removed.",
    label: "Clear AI index",
  },
  reset: {
    title: "Reset database",
    confirm: "RESET DATABASE",
    description:
      "Remove the local database contents, including libraries, identities, and corrections. This cannot be undone. Original files remain untouched; add your libraries and scan again to rebuild.",
    label: "Reset database",
  },
} as const;

export function Settings() {
  const resource = useResource<SettingsData>("/settings");
  const engine = useResource<Engine>("/engine");
  const [operation, setOperation] = useState<keyof typeof maintenance | null>(
    null,
  );
  const [reconcile, setReconcile] = useState(false);
  const { job, setTheme } = useApp();
  const active = !!job && ["running", "queued", "paused"].includes(job.status);
  useEffect(() => {
    if (engine.data?.state !== "loading") return;
    const timer = setTimeout(engine.reload, 2000);
    return () => clearTimeout(timer);
  }, [engine.data]);
  return (
    <>
      <PageHeader
        eyebrow="THIS IS YOUR SPACE"
        title="Make room, your way."
        description="Manage your local libraries, recognition engine, and the little details."
      />
      <ErrorNotice error={resource.error} retry={resource.reload} />
      {resource.loading && !resource.data && (
        <Loading label="Loading local settings" />
      )}
      <div className="settings-layout">
        <div className="settings-primary">
          <Libraries roots={resource.data?.roots ?? []} />
          <section className="settings-section">
            <div className="section-heading">
              <div>
                <p className="eyebrow">EVERYTHING RUNS HERE</p>
                <h2>Indexing & local engine</h2>
              </div>
            </div>
            <JobCard />
            {!job && (
              <p className="settings-empty">
                No indexing jobs yet. Add a library, load the local model, and
                start a scan.
              </p>
            )}
            {active && (
              <p className="small-text muted">
                Only one job runs at a time. Progress streams live from the local
                server.
              </p>
            )}
            <ErrorNotice error={engine.error} retry={engine.reload} />
            {engine.data && (
              <>
                <EngineStatus engine={engine.data} />
                <dl className="metadata provider-metadata">
                  <div>
                    <dt>Active provider</dt>
                    <dd>{engine.data.provider ?? "Not loaded"}</dd>
                  </div>
                  <div>
                    <dt>Available providers</dt>
                    <dd>
                      {engine.data.available_providers.length
                        ? engine.data.available_providers.join(", ")
                        : "None reported"}
                    </dd>
                  </div>
                  <div>
                    <dt>Model</dt>
                    <dd>{engine.data.model}</dd>
                  </div>
                </dl>
                <p className="small-text muted">
                  Provider selection is managed by the local engine. Models must
                  be installed locally; loading never sends your photos to a
                  service.
                </p>
              </>
            )}
            <div className="reconcile-row">
              <div>
                <h3>Reconcile identities</h3>
                <p>Run global clustering with manual corrections protected.</p>
              </div>
              <button
                className="button small"
                disabled={active}
                onClick={() => setReconcile(true)}
              >
                <Icon name="merge" size={16} />
                Reconcile
              </button>
            </div>
          </section>
          <ModelRuntime />
          <VlmCard />
          <AppLockCard />
          {resource.data && <MapAndLanguage settings={resource.data} />}
          {resource.data && <Preferences settings={resource.data} />}
          <Exclusions />
        </div>
        <aside className="settings-secondary">
          <section className="privacy-card">
            <Icon name="shield" size={30} />
            <p className="eyebrow">LOCAL BY DESIGN</p>
            <h2>Some things should stay yours.</h2>
            <p>No account. No upload. No face data leaving your machine.</p>
            <ul>
              <li>Originals stay in place</li>
              <li>Corrections are yours</li>
              <li>Export whenever you want</li>
            </ul>
          </section>
          {resource.data && (
            <section className="storage-card">
              <h3>Local storage</h3>
              <dl className="metadata">
                <div>
                  <dt>Database</dt>
                  <dd>{bytes(resource.data.storage_bytes?.database ?? NaN)}</dd>
                </div>
                <div>
                  <dt>Embeddings</dt>
                  <dd>{bytes(resource.data.storage_bytes?.embeddings ?? NaN)}</dd>
                </div>
                <div>
                  <dt>Thumbnails</dt>
                  <dd>{bytes(resource.data.storage_bytes?.thumbnails ?? NaN)}</dd>
                </div>
              </dl>
              <p className="small-text muted">
                Index storage only. Original media is not included.
              </p>
            </section>
          )}
          <section className="maintenance-card">
            <p className="eyebrow">HANDLE WITH CARE</p>
            <h3>Maintenance</h3>
            <p className="small-text muted">
              These actions affect the local index, never your original files.
              Finish any running scan first.
            </p>
            {Object.entries(maintenance).map(([key, item]) => (
              <button
                key={key}
                className={`button ${key === "index" || key === "reset" ? "danger-text" : ""}`}
                disabled={active}
                onClick={() => setOperation(key as keyof typeof maintenance)}
              >
                {item.title}
                <Icon name="arrow" size={14} />
              </button>
            ))}
          </section>
        </aside>
      </div>
      {operation && (
        <ConfirmDialog
          open
          onClose={() => setOperation(null)}
          title={maintenance[operation].title}
          description={maintenance[operation].description}
          confirmation={maintenance[operation].confirm}
          label={maintenance[operation].label}
          onConfirm={async () => {
            await mutate("/maintenance", {
              action: operation,
              confirm: maintenance[operation].confirm,
            });
            if (operation === "reset") setTheme("system");
          }}
          success="Maintenance completed."
        />
      )}
      <ConfirmDialog
        open={reconcile}
        onClose={() => setReconcile(false)}
        title="Reconcile face identities?"
        description="Run bounded global clustering over the local face index. Your manual moves, rejections, and keep-separate corrections are protected. Track progress in the indexing panel."
        label="Start reconciliation"
        danger={false}
        onConfirm={() => mutate("/index/reconcile")}
        success="Reconciliation started."
      />
    </>
  );
}

interface ModelStatus {
  provider_order: string[];
  idle_seconds: number;
  models: { key: string; role: string; installed: boolean; license: string;
    slots: { name: string; loaded: boolean; provider_label: string | null; failed_providers: string[]; error: string | null }[] }[];
}

/** Which ONNX models are installed and the execution provider each one runs on. */
function ModelRuntime() {
  const status = useResource<ModelStatus>("/models");
  return (
    <section className="settings-section" aria-labelledby="models-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">SEARCH & SIMILARITY MODELS</p>
          <h2 id="models-heading">AI models on this device</h2>
        </div>
      </div>
      <ErrorNotice error={status.error} retry={status.reload} />
      {status.data && (
        <>
          <dl className="metadata provider-metadata">
            <div><dt>Provider order</dt><dd>{status.data.provider_order.join(" → ") || "CPU"}</dd></div>
            <div><dt>Unload when idle</dt><dd>{Math.round(status.data.idle_seconds / 60)} min</dd></div>
            {status.data.models.map((model) => (
              <div key={model.key}>
                <dt>{model.key} <span className="muted small-text">({model.role}, {model.license})</span></dt>
                <dd>
                  {!model.installed ? "Not installed" : model.slots.map((slot) =>
                    `${slot.name}: ${slot.loaded ? slot.provider_label ?? "loaded" : "idle"}${slot.failed_providers.length ? ` (fell back from ${slot.failed_providers.join(", ")})` : ""}`).join(" · ")}
                </dd>
              </div>
            ))}
          </dl>
          {status.data.models.some((model) => !model.installed) && (
            <p className="small-text muted">
              Install once with <code>python scripts/fetch_models.py</code>. Nothing is downloaded automatically.
            </p>
          )}
        </>
      )}
    </section>
  );
}

/** Map base-map (local PMTiles only, off by default) and interface language. */
function MapAndLanguage({ settings }: { settings: SettingsData }) {
  const t = useT();
  const action = useAction();
  const extra = settings as SettingsData & { map_tiles_enabled?: boolean; map_pmtiles_path?: string };
  const [path, setPath] = useState(extra.map_pmtiles_path ?? "");
  const [lang, setLang] = useState(getLocale());
  return (
    <section className="settings-section" aria-labelledby="map-settings-heading">
      <div className="section-heading"><div><h2 id="map-settings-heading">{t("settings.mapTitle")}</h2><p>{t("settings.mapHelp")}</p></div></div>
      <label className="check-label">
        <input type="checkbox" checked={!!extra.map_tiles_enabled} disabled={action.busy}
          onChange={(event) => void action.run(() => mutate("/settings", { map_tiles_enabled: event.target.checked }, "PATCH"))} />
        {t("settings.mapTiles")}
      </label>
      <form className="inline-actions" onSubmit={(event) => {
        event.preventDefault();
        void action.run(() => mutate("/settings", { map_pmtiles_path: path, map_tiles_enabled: true }, "PATCH"), "Map file saved.");
      }}>
        <label className="field wide-field">{t("settings.mapPath")}
          <input value={path} onChange={(event) => setPath(event.target.value)} placeholder="/Users/me/Maps/world.pmtiles" spellCheck={false} />
        </label>
        <button className="button small" disabled={!path.trim() || action.busy}>Save</button>
      </form>
      <ErrorNotice error={action.error} />
      <label className="field">{t("common.language")}
        <select value={lang} onChange={(event) => { const next = event.target.value as Locale; setLang(next); setLocale(next); }}>
          {LOCALES.map((item) => <option key={item.value} value={item.value} lang={item.value}>{item.label}</option>)}
        </select>
      </label>
    </section>
  );
}

interface VlmStatus {
  enabled: boolean; model: string; license: string; installed: boolean; loaded: boolean; disk_bytes: number;
  ram_bytes: number; ram_cap_bytes: number; device: string | null; idle_seconds: number; install_hint: string;
}

/** Optional local vision-language model: off by default, never loaded unless enabled here. */
function VlmCard() {
  const t = useT();
  const status = useResource<VlmStatus>("/vlm");
  const action = useAction();
  const s = status.data;
  return (
    <section className="settings-section" aria-labelledby="vlm-heading">
      <div className="section-heading"><div><p className="eyebrow">{t("vlm.eyebrow")}</p><h2 id="vlm-heading">{t("vlm.title")}</h2><p>{t("vlm.help")}</p></div></div>
      <ErrorNotice error={status.error || action.error} retry={status.reload} />
      {s && (
        <>
          <label className="check-label">
            <input type="checkbox" checked={s.enabled} disabled={action.busy || (!s.installed && !s.enabled)}
              onChange={(event) => void action.run(() => mutate("/settings", { vlm_enabled: event.target.checked }, "PATCH"))} />
            {t("vlm.enable")}
          </label>
          <dl className="metadata provider-metadata">
            <div><dt>{t("vlm.model")}</dt><dd>{s.model} · {s.license}</dd></div>
            <div><dt>{t("vlm.size")}</dt><dd>{s.installed ? bytes(s.disk_bytes) : t("vlm.notInstalled")}</dd></div>
            <div><dt>{t("vlm.memory")}</dt><dd>{s.loaded ? `${bytes(s.ram_bytes)} / ${bytes(s.ram_cap_bytes)}` : t("vlm.notLoaded")}</dd></div>
            <div><dt>{t("vlm.device")}</dt><dd>{s.device ?? "–"}</dd></div>
          </dl>
          {!s.installed && <p className="small-text muted">{t("vlm.install")} <code>{s.install_hint}</code></p>}
          {s.enabled && s.installed && (
            <div className="inline-actions">
              <button className="button small" disabled={action.busy || s.loaded} onClick={() => void action.run(() => mutate("/vlm/load"))}>{t("vlm.load")}</button>
              <button className="button small" disabled={action.busy || !s.loaded} onClick={() => void action.run(() => mutate("/vlm/unload"))}>{t("vlm.unload")}</button>
              <span className="small-text muted">{t("vlm.idle", { minutes: Math.round(s.idle_seconds / 60) })}</span>
            </div>
          )}
        </>
      )}
    </section>
  );
}
