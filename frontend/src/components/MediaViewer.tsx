import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { dateLabel, exportZip, mutate, percent, timeLabel } from "../api";
import { useAction, useApp } from "../context";
import { useResource } from "../hooks";
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

export function MediaViewer({
  id,
  ids = [],
  timestamp,
  onClose,
}: {
  id: number;
  ids?: number[];
  timestamp?: number | null;
  onClose: () => void;
}) {
  const [current, setCurrent] = useState(id);
  const resource = useResource<MediaDetail>(`/media/${current}`);
  const media = resource.data;
  const [selected, setSelected] = useState<number | null>(null);
  const [boxes, setBoxes] = useState(true);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [previewError, setPreviewError] = useState(false);
  const [time, setTime] = useState(0);
  const video = useRef<HTMLVideoElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const pendingSeek = useRef<number | null>(timestamp ?? null);
  const action = useAction();
  const { notify } = useApp();
  const position = ids.indexOf(current);
  useEffect(() => {
    setCurrent(id);
    pendingSeek.current = timestamp ?? null;
  }, [id, timestamp]);
  useEffect(() => {
    setSelected(null);
    setPreviewError(false);
    setTime(0);
  }, [current]);
  const go = (offset: number) => {
    const next = ids[position + offset];
    if (next !== undefined) {
      pendingSeek.current = null;
      setCurrent(next);
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
              (media.missing || previewError ? (
                <div className="viewer-unavailable">
                  <Icon name="alert" size={40} />
                  <h3>
                    {media.missing
                      ? "Original file is missing"
                      : "Preview unavailable"}
                  </h3>
                  <p>
                    {media.missing
                      ? "Reconnect the library drive or restore the file to its original location, then run a cleanup check."
                      : "The browser could not display this file. Try downloading the original or checking the library."}
                  </p>
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
                      key={media.id}
                      ref={video}
                      controls
                      playsInline
                      preload="metadata"
                      src={`/api/media/${media.id}/file`}
                      poster={`/api/media/${media.id}/thumbnail`}
                      onError={() => setPreviewError(true)}
                      onTimeUpdate={(event) =>
                        setTime(event.currentTarget.currentTime)
                      }
                      onLoadedMetadata={(event) => {
                        if (pendingSeek.current !== null) {
                          event.currentTarget.currentTime = pendingSeek.current;
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
                </dl>
              </section>
              <div className="viewer-file-actions">
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
                    Delete from index
                  </button>
                )}
                <p className="small-text muted">
                  Your original files are never removed.
                </p>
              </div>
            </>
          )}
        </aside>
      </div>
      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title="Delete this media from the index?"
        description="The original file stays exactly where it is. You can restore this indexed record from the deleted media filter."
        label="Delete from index"
        onConfirm={() => mutate(`/media/${current}`, undefined, "DELETE")}
        success="Media deleted from the index, not from disk."
      />
    </Dialog>
  );
}
