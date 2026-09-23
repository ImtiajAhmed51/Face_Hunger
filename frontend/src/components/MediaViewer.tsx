import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { dateLabel, exportZip, mutate, percent, timeLabel } from "../api";
import { useAction, useApp } from "../context";
import { useMounted, useResource } from "../hooks";
import type { Face, MediaDetail } from "../types";
import { FaceActions } from "./FaceActions";
import { Icon } from "./Icon";
import {
  Badge,
  ConfirmDialog,
  Dialog,
  ErrorNotice,
  Loading,
  Thumbnail,
} from "./ui";

type ViewerProps = { id: number; ids?: number[]; timestamp?: number | null; onClose: () => void };

export function MediaViewer(props: ViewerProps) {
  const [navigation, setNavigation] = useState({ source: props.id, current: props.id, timestamp: props.timestamp });
  const current = navigation.source === props.id ? navigation.current : props.id;
  const timestamp = navigation.source === props.id ? navigation.timestamp : props.timestamp;
  return <ViewerSession {...props} key={current} id={current} timestamp={timestamp}
    onNavigate={id => setNavigation({ source: props.id, current: id, timestamp: undefined })} />;
}

function ViewerSession({ id, ids = [], timestamp, onClose, onNavigate }: ViewerProps & { onNavigate: (id: number) => void }) {
  const current = id;
  const mounted = useMounted();
  const convertLock = useRef(false);
  const resource = useResource<MediaDetail>(`/media/${current}`);
  const media = resource.data;
  const [selected, setSelected] = useState<number | null>(null);
  const [boxes, setBoxes] = useState(true);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [previewError, setPreviewError] = useState(false);
  const [converting, setConverting] = useState(false);
  const [convertProgress, setConvertProgress] = useState(0);
  const [convertStage, setConvertStage] = useState("Preparing video…");
  const [convertIndeterminate, setConvertIndeterminate] = useState(true);
  const [convertError, setConvertError] = useState<string | null>(null);
  const [videoReady, setVideoReady] = useState(false);
  const [videoSrcVersion, setVideoSrcVersion] = useState(0);
  const [time, setTime] = useState(0);
  const [needsConvert, setNeedsConvert] = useState(false);
  const [convertStarted, setConvertStarted] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const pendingSeek = useRef<number | null>(timestamp ?? null);
  const action = useAction();
  const { notify } = useApp();
  const position = ids.indexOf(current);
  useEffect(() => {
    pendingSeek.current = timestamp ?? null;
    if (timestamp != null && video.current && video.current.readyState >= 1) {
      video.current.currentTime = Math.max(0, Math.min(timestamp, video.current.duration || timestamp));
      pendingSeek.current = null;
    }
  }, [timestamp]);
  const attachVideo = useCallback((player: HTMLVideoElement | null) => {
    video.current = player;
    if (!player) return;
    return () => {
      player.pause();
      player.removeAttribute('src');
      player.load();
      if (video.current === player) video.current = null;
    };
  }, []);
  useEffect(() => {
    setSelected(null);
    setPreviewError(false);
    setConverting(false);
    setConvertProgress(0);
    setConvertStage("Preparing video…");
    setConvertIndeterminate(true);
    setConvertError(null);
    setVideoReady(false);
    setVideoSrcVersion(0);
    setTime(0);
    setNeedsConvert(false);
    setConvertStarted(false);
  }, [current]);

  // Status check only — never auto-start conversion
  useEffect(() => {
    if (!media || media.kind !== "video" || media.missing) return;
    let cancelled = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;

    const applyStatus = (data: {
      status?: string;
      progress?: number;
      stage?: string;
      ready?: boolean;
      error?: string | null;
      indeterminate?: boolean;
      needs_conversion?: boolean;
    }) => {
      const status = data.status || "";
      if (status === "completed" || status === "ready" || data.ready) {
        setConverting(false);
        setConvertStarted(false);
        setNeedsConvert(false);
        setConvertProgress(100);
        setConvertStage("Video ready");
        setConvertIndeterminate(false);
        setConvertError(null);
        setPreviewError(false);
        setVideoReady(true);
        setVideoSrcVersion(1);
        return "done";
      }
      if (status === "failed") {
        setConverting(false);
        setConvertStarted(false);
        setConvertError(
          data.error ||
            "Unable to convert this video. The original file has been preserved.",
        );
        setVideoReady(false);
        setNeedsConvert(Boolean(data.needs_conversion));
        return "done";
      }
      // Only an in-progress job (or one the user just started) shows the conversion panel.
      // Bare "pending"/"idle" from a status check must NOT look like conversion is running.
      const activeJob = ["analyzing", "converting", "verifying", "replacing"].includes(
        status,
      );
      if (activeJob || convertStarted) {
        setConverting(true);
        setVideoReady(false);
        setNeedsConvert(false);
        setConvertStage(data.stage || "Preparing video…");
        if (typeof data.progress === "number") {
          setConvertProgress(Math.max(0, Math.min(100, data.progress)));
        }
        setConvertIndeterminate(
          Boolean(data.indeterminate) ||
            !(typeof data.progress === "number" && data.progress > 0),
        );
        return "continue";
      }
      // Idle / conversion available: play original; Convert only on explicit click
      setConverting(false);
      setNeedsConvert(Boolean(data.needs_conversion));
      setVideoReady(true);
      return "done";
    };

    const poll = async () => {
      if (cancelled) return;
      if (document.hidden) { timer = setTimeout(poll, 2000); return; }
      try {
        const res = await fetch(`/api/media/${media.id}/conversion-status`, { signal: controller.signal });
        if (!res.ok) {
          if (!cancelled) {
            if (convertStarted) timer = setTimeout(poll, 3000);
            else { setVideoReady(true); setConverting(false); }
          }
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        const next = applyStatus(data);
        if (next === "continue") {
          timer = setTimeout(poll, 1000);
        }
      } catch {
        if (!cancelled) {
          if (convertStarted) timer = setTimeout(poll, 3000);
          else { setVideoReady(true); setConverting(false); }
        }
      }
    };

    setConvertError(null);
    void poll();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [media?.id, media?.kind, media?.missing, convertStarted]);

  const startConvert = () => {
    // Conversion starts only from this explicit user action — never on open/click.
    if (!media || convertLock.current || converting) return;
    convertLock.current = true;
    setConverting(true);
    setConvertStarted(true);
    setNeedsConvert(false);
    setConvertError(null);
    setVideoReady(false);
    setConvertStage("Starting conversion…");
    setConvertIndeterminate(true);
    void (async () => {
      try {
        await mutate(`/media/${media.id}/convert`, {});
      } catch (error) {
        if (!mounted.current) return;
        setConvertStarted(false);
        setConverting(false);
        setNeedsConvert(true);
        setConvertError(
          error instanceof Error ? error.message : "Could not start conversion",
        );
        setVideoReady(true);
      } finally {
        convertLock.current = false;
      }
    })();
  };
  const go = (offset: number) => {
    const next = ids[position + offset];
    if (next !== undefined) {
      pendingSeek.current = null;
      onNavigate(next);
    }
  };
  const fullscreen = async () => {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
        return;
      }
      // Prefer the dialog; fall back to stage / documentElement for browsers that
      // reject requestFullscreen() on <dialog> (Safari, some Chromium builds).
      const dialog = stage.current?.closest("dialog") as HTMLElement | null;
      const targets: (HTMLElement | null | undefined)[] = [
        dialog,
        stage.current,
        document.documentElement,
      ];
      let lastError: unknown;
      for (const target of targets) {
        if (!target || typeof target.requestFullscreen !== "function") continue;
        try {
          await target.requestFullscreen();
          return;
        } catch (error) {
          lastError = error;
        }
      }
      throw lastError ?? new Error("Fullscreen API unavailable");
    } catch {
      notify(
        "Fullscreen is not available in this browser. The expanded viewer remains available.",
        true,
      );
    }
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const dialogs = document.querySelectorAll("dialog[open]");
      if (!dialogs[dialogs.length - 1]?.classList.contains("viewer-dialog"))
        return;
      if (
        event.target instanceof HTMLElement &&
        event.target.closest(
          "input,textarea,select,video,[contenteditable=true]",
        )
      )
        return;
      if (event.key === "ArrowLeft" && position > 0) {
        event.preventDefault();
        go(-1);
      }
      if (event.key === "ArrowRight" && position < ids.length - 1) {
        event.preventDefault();
        go(1);
      }
      if (event.key.toLowerCase() === "f") {
        event.preventDefault();
        void fullscreen();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });
  const pick = (face: Face) => {
    setSelected(face.id);
    if (face.timestamp !== null) {
      if (video.current && video.current.readyState >= 1)
        video.current.currentTime = face.timestamp;
      else pendingSeek.current = face.timestamp;
      setTime(face.timestamp);
    }
  };
  const face = media?.faces.find((item) => item.id === selected);
  const visibleFaces =
    media?.faces.filter(
      (item) =>
        !item.deleted_at &&
        (media.kind === "photo" ||
          (item.timestamp !== null && Math.abs(item.timestamp - time) < 0.6)),
    ) ?? [];
  return (
    <Dialog
      open
      title={media?.name ?? "Media viewer"}
      onClose={onClose}
      className="viewer-dialog"
    >
      <div className="viewer-layout">
        <div className="viewer-main">
          <div className="viewer-toolbar">
            <span className="muted">
              {position >= 0
                ? `${position + 1} of ${ids.length} on this page`
                : "Source media"}
            </span>
            <div className="inline-actions">
              <button
                className="button small"
                onClick={() => setBoxes((value) => !value)}
                aria-pressed={boxes}
              >
                <Icon name="people" size={16} />
                Face boxes
              </button>
              <button
                className="icon-button"
                aria-label="Toggle fullscreen (F)"
                onClick={() => void fullscreen()}
              >
                <Icon name="expand" />
              </button>
            </div>
          </div>
          <div className="viewer-stage" ref={stage}>
            {resource.loading && !media && (
              <Loading label="Opening original media" />
            )}
            {resource.error && (
              <ErrorNotice error={resource.error} retry={resource.reload} />
            )}
            {media &&
              (media.missing || convertError || (previewError && (media.kind === "photo" || (!converting && videoReady && !needsConvert))) ? (
                <div className="viewer-unavailable">
                  <Icon name="alert" size={40} />
                  <h3>
                    {media.missing
                      ? "Original file is missing"
                      : convertError
                        ? "Video conversion failed"
                        : "Preview unavailable"}
                  </h3>
                  <p>
                    {media.missing
                      ? "Reconnect the library drive or restore the file to its original location, then run a cleanup check."
                      : convertError
                        ? convertError
                        : "The browser could not display this file. Try downloading the original or checking the library."}
                  </p>
                  {convertError && !media.missing && <button className="button primary" onClick={startConvert}>Retry conversion</button>}
                  {!media.missing && (
                    <a
                      className="button"
                      href={`/api/media/${media.id}/file`}
                      download
                    >
                      Download original
                    </a>
                  )}
                </div>
              ) : converting && media.kind === "video" && !videoReady ? (
                <div className="conversion-panel">
                  <div className="conversion-card">
                    <div className="conversion-thumb">
                      <Thumbnail
                        src={`/api/media/${media.id}/thumbnail`}
                        alt={media.name}
                      />
                    </div>
                    <div className="conversion-body">
                      <p className="conversion-eyebrow">Processing video</p>
                      <h3 className="conversion-title">{media.name}</h3>
                      <p className="conversion-stage">{convertStage}</p>
                      <div className="conversion-progress-track">
                        <div
                          className={`conversion-progress-fill${convertIndeterminate ? " indeterminate" : ""}`}
                          style={
                            convertIndeterminate
                              ? undefined
                              : { width: `${Math.max(4, convertProgress)}%` }
                          }
                        />
                      </div>
                      <div className="conversion-meta">
                        {!convertIndeterminate && convertProgress > 0 ? (
                          <strong>{Math.round(convertProgress)}%</strong>
                        ) : (
                          <span className="conversion-pulse">Working…</span>
                        )}
                        <span>Please wait — the original is kept until conversion succeeds.</span>
                      </div>
                    </div>
                  </div>
                </div>
              ) : needsConvert && previewError && media.kind === "video" && !converting ? (
                <div className="conversion-panel">
                  <div className="conversion-card">
                    <div className="conversion-thumb">
                      <Thumbnail
                        src={`/api/media/${media.id}/thumbnail`}
                        alt={media.name}
                      />
                    </div>
                    <div className="conversion-body">
                      <p className="conversion-eyebrow">Conversion available</p>
                      <h3 className="conversion-title">{media.name}</h3>
                      <p className="conversion-stage">
                        This format may not play in the browser. Convert only when you choose —
                        the original file is kept as a backup.
                      </p>
                      <div className="inline-actions" style={{ marginTop: "0.75rem" }}>
                        <button className="button primary" type="button" onClick={startConvert}>
                          <Icon name="spark" size={16} />
                          Convert for browser
                        </button>
                        <a className="button subtle" href={`/api/media/${media.id}/file`} download>
                          Download original
                        </a>
                      </div>
                    </div>
                  </div>
                </div>
              ) : media.kind === "video" && !videoReady ? (
                <Loading label="Checking video playback" />
              ) : (
                <div
                  className="media-surface"
                  style={{
                    aspectRatio:
                      media.width && media.height
                        ? `${media.width} / ${media.height}`
                        : undefined,
                    maxWidth:
                      media.width && media.height
                        ? `min(100%, ${(68 * media.width) / media.height}vh)`
                        : "100%",
                  }}
                >
                  {media.kind === "photo" ? (
                    <img
                      className="viewer-photo"
                      src={`/api/media/${media.id}/preview`}
                      alt={media.name}
                      onError={() => setPreviewError(true)}
                    />
                  ) : (
                    <video
                      key={`${media.id}-${videoSrcVersion}`}
                      ref={attachVideo}
                      controls
                      playsInline
                      preload="metadata"
                      src={
                        videoReady || !converting
                          ? `/api/media/${media.id}/file?v=${videoSrcVersion}`
                          : undefined
                      }
                      poster={`/api/media/${media.id}/thumbnail`}
                      onError={() => {
                        if (!converting) {
                          setNeedsConvert(true);
                          setPreviewError(true);
                        }
                      }}
                      onTimeUpdate={(event) => {
                        if (boxes) setTime(Math.round(event.currentTarget.currentTime * 5) / 5);
                      }}
                      onLoadedMetadata={(event) => {
                        setConverting(false);
                        setPreviewError(false);
                        setVideoReady(true);
                        if (pendingSeek.current !== null) {
                          const duration = event.currentTarget.duration;
                          event.currentTarget.currentTime = Math.max(0, Math.min(pendingSeek.current, Number.isFinite(duration) ? duration : pendingSeek.current));
                          pendingSeek.current = null;
                        }
                      }}
                      aria-label={media.name}
                    />
                  )}
                  {boxes && !!media.width && !!media.height && (
                    <div className="face-overlay">
                      {visibleFaces.map((item) => (
                        <button
                          key={item.id}
                          className={`face-box ${item.id === selected ? "active" : ""}`}
                          style={{
                            left: `${(item.bbox[0] / media.width!) * 100}%`,
                            top: `${(item.bbox[1] / media.height!) * 100}%`,
                            width: `${(item.bbox[2] / media.width!) * 100}%`,
                            height: `${(item.bbox[3] / media.height!) * 100}%`,
                          }}
                          onClick={() => pick(item)}
                          aria-label={`Inspect ${item.display_name}, face ${item.id}`}
                          aria-pressed={item.id === selected}
                          title={item.display_name}
                        >
                          <span>{item.display_name}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
          </div>
          <div className="viewer-bottom">
            <button
              className="button small"
              disabled={position <= 0}
              onClick={() => go(-1)}
            >
              <Icon name="back" size={16} />
              Previous
            </button>
            <span className="small-text muted">
              Arrow keys to browse · F for fullscreen
            </span>
            <button
              className="button small"
              disabled={position < 0 || position >= ids.length - 1}
              onClick={() => go(1)}
            >
              Next
              <Icon name="arrow" size={16} />
            </button>
          </div>
        </div>
        <aside className="viewer-details" aria-label="Media details">
          {media && (
            <>
              <div className="badge-row">
                <Badge>{media.kind}</Badge>
                <Badge
                  tone={media.status === "failed" || media.missing ? "red" : ""}
                >
                  {media.status}
                </Badge>
                {media.deleted_at && (
                  <Badge tone="red">Deleted from index</Badge>
                )}
              </div>
              <h3>In this {media.kind === "photo" ? "photo" : "video"}</h3>
              {!media.faces.length && (
                <p className="muted">No indexed faces in this file.</p>
              )}
              <div className="viewer-faces">
                {media.faces.map((item) => (
                  <button
                    className={`viewer-face ${selected === item.id ? "active" : ""}`}
                    key={item.id}
                    onClick={() => pick(item)}
                    aria-pressed={selected === item.id}
                  >
                    <Thumbnail
                      src={`/api/faces/${item.id}/thumbnail`}
                      alt=""
                      icon="people"
                    />
                    <span>
                      <strong>{item.display_name || "Unassigned"}</strong>
                      <small>
                        {item.timestamp !== null
                          ? `${timeLabel(item.timestamp)} · `
                          : ""}
                        {item.deleted_at ? "Deleted" : item.review_state}
                        {item.excluded ? " · Excluded" : ""}
                      </small>
                    </span>
                  </button>
                ))}
              </div>
              {face && (
                <section className="face-inspector">
                  <div className="section-heading">
                    <h4>Face #{face.id}</h4>
                    {face.person_id && (
                      <Link
                        to={`/people/${face.person_id}`}
                        onClick={onClose}
                        className="text-link"
                      >
                        Profile
                        <Icon name="arrow" size={14} />
                      </Link>
                    )}
                  </div>
                  <dl className="metadata">
                    <div>
                      <dt>Detection</dt>
                      <dd>{percent(face.detection)}</dd>
                    </div>
                    <div>
                      <dt>Identity similarity</dt>
                      <dd>{percent(face.similarity)}</dd>
                    </div>
                    <div>
                      <dt>Confidence</dt>
                      <dd>{face.confidence_label}</dd>
                    </div>
                    <div>
                      <dt>Review</dt>
                      <dd>{face.review_state}</dd>
                    </div>
                    {face.timestamp !== null && (
                      <div>
                        <dt>Video timestamp</dt>
                        <dd>{face.timestamp.toFixed(2)}s</dd>
                      </div>
                    )}
                  </dl>
                  <FaceActions faces={[face]} allowPermanent />
                </section>
              )}
              <section className="metadata-section">
                <h3>File details</h3>
                <dl className="metadata">
                  <div>
                    <dt>Name</dt>
                    <dd>{media.name}</dd>
                  </div>
                  <div>
                    <dt>Captured</dt>
                    <dd>{dateLabel(media.captured_at)}</dd>
                  </div>
                  <div>
                    <dt>Dimensions</dt>
                    <dd>
                      {media.width && media.height
                        ? `${media.width} × ${media.height}`
                        : "Unknown"}
                    </dd>
                  </div>
                  {media.kind === "video" && (
                    <div>
                      <dt>Duration</dt>
                      <dd>{timeLabel(media.duration)}</dd>
                    </div>
                  )}
                  <div>
                    <dt>Indexed faces</dt>
                    <dd>{media.face_count}</dd>
                  </div>
                  <div>
                    <dt>Record</dt>
                    <dd>#{media.id}</dd>
                  </div>
                  {media.deleted_at && (
                    <div>
                      <dt>Deleted</dt>
                      <dd>{dateLabel(media.deleted_at)}</dd>
                    </div>
                  )}
                  {media.has_original && (
                    <div>
                      <dt>Pre-conversion original</dt>
                      <dd title={media.original_path || undefined}>
                        {media.original_name || "Soft-kept on disk"}
                      </dd>
                    </div>
                  )}
                </dl>
              </section>
              <div className="viewer-file-actions">
                {media.kind === "video" && needsConvert && !converting && !media.missing && (
                  <button
                    className="button primary"
                    type="button"
                    onClick={startConvert}
                  >
                    <Icon name="spark" size={16} />
                    Convert for browser
                  </button>
                )}
                {media.kind === "video" && converting && (
                  <p className="small-text muted" role="status">
                    {convertStage}
                    {!convertIndeterminate && convertProgress > 0
                      ? ` · ${Math.round(convertProgress)}%`
                      : ""}
                  </p>
                )}
                {media.has_original && (
                  <a
                    className="button"
                    href={`/api/media/${media.id}/original`}
                    download
                  >
                    <Icon name="download" size={16} />
                    Download original file
                  </a>
                )}
                <button
                  className="button"
                  disabled={action.busy || media.missing}
                  onClick={() =>
                    void action.run(
                      () => exportZip("/export", { media_ids: [media.id] }),
                      "Export downloaded.",
                      false,
                    )
                  }
                >
                  <Icon name="download" size={16} />
                  Export original + manifest
                </button>
                {media.deleted_at ? (
                  <button
                    className="button"
                    disabled={action.busy}
                    onClick={() =>
                      void action.run(
                        () => mutate(`/media/${media.id}/restore`),
                        "Media restored to the index.",
                      )
                    }
                  >
                    <Icon name="restore" size={16} />
                    Restore media
                  </button>
                ) : (
                  <button
                    className="button danger-text"
                    disabled={action.busy}
                    onClick={() => setDeleteOpen(true)}
                  >
                    <Icon name="trash" size={16} />
                    Delete
                  </button>
                )}
                <p className="small-text muted">
                  Soft-deletes only. Originals stay on disk until permanently removed from the Deleted tab.
                </p>
              </div>
            </>
          )}
        </aside>
      </div>
      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title="Move this media to Deleted?"
        description="The original file stays on disk. Restore it anytime from the Deleted tab, or permanently remove it there."
        label="Delete"
        onConfirm={() => mutate(`/media/${current}`, undefined, "DELETE")}
        success="Media moved to Deleted. Original file unchanged."
      />
    </Dialog>
  );
}
