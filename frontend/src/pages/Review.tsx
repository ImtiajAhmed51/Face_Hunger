import { Button } from "@/components/primitives/button";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { mutate, number, percent, queryString, timeLabel } from "../api";
import { useAction, useApp } from "../context";
import { useAnimatedList, useResource } from "../hooks";
import type { Face, Page } from "../types";
import { MoveFacesDialog } from "../components/FaceActions";
import { Icon } from "../components/Icon";
import { MediaViewer } from "../components/MediaViewer";
import { PersonPicker, PersonToken } from "../components/PersonPicker";
import {
  Badge,
  ConfirmDialog,
  Dialog,
  Empty,
  ErrorNotice,
  Loading,
  PageHeader,
  Pagination,
  Thumbnail,
} from "../components/ui";

const PAGE_LIMIT = 30;

export function Review() {
  const [params, setParams] = useSearchParams();
  const personId = Number(params.get("person_id")) || undefined;
  const [page, setPage] = useState(1);
  const [cursor, setCursor] = useState(0);
  // Bounded set of dismissed face ids for the current page only.
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());
  const resource = useResource<Page<Face>>(
    `/review?${queryString({ page, limit: PAGE_LIMIT, person_id: personId, deleted: false })}`,
  );
  const training = useResource<{
    active: boolean; state_note: string; samples: number; positive_samples: number; negative_samples: number;
    minimum_samples: number; last_training: { reason: string; validation?: { precision: number; recall: number } } | null;
  }>("/review/training");
  const [trainingMessage, setTrainingMessage] = useState("");
  useEffect(() => {
    if (!dismissed.size) return;
    const timer = window.setTimeout(training.reload, 1500);
    return () => window.clearTimeout(timer);
  }, [dismissed.size, training.reload]);
  const [viewer, setViewer] = useState<Face | null>(null);
  const [dialog, setDialog] = useState<"delete" | "move" | "person" | null>(
    null,
  );
  const action = useAction();
  const { pushUndo, undoLast, canUndo } = useApp();
  // In-flight review IDs — allow rapid Yes/No without waiting for each HTTP round-trip
  const pendingReviews = useRef<Set<number>>(new Set());
  // Prevent concurrent auto-advance reloads
  const advancing = useRef(false);
  const items =
    resource.data?.items.filter((face) => !dismissed.has(face.id)) ?? [];
  const animatedItems = useAnimatedList(items, (face) => face.id);
  const face = items[Math.min(cursor, Math.max(0, items.length - 1))] ?? null;
  const faceRef = useRef(face);
  faceRef.current = face;
  const itemsLenRef = useRef(items.length);
  itemsLenRef.current = items.length;
  const dialogRef = useRef(dialog);
  dialogRef.current = dialog;
  const viewerRef = useRef(viewer);
  viewerRef.current = viewer;

  useEffect(() => {
    setCursor(0);
    setDismissed(new Set());
    pendingReviews.current.clear();
    advancing.current = false;
  }, [page, personId]);

  useEffect(() => {
    setCursor((value) => Math.min(value, Math.max(0, items.length - 1)));
  }, [items.length]);

  const reloadQueue = resource.reload;
  const queueTotal = resource.data?.total ?? 0;
  const queueLoading = resource.loading || resource.refreshing;

  // When the current page is fully reviewed, reload page 1 of the queue.
  // Reviews shrink the server-side total, so advancing offset would skip faces;
  // always re-fetch the head of the queue for continuous Yes/Select.
  useEffect(() => {
    if (queueLoading) {
      // Reload in flight — keep advancing lock until data lands
      return;
    }
    if (advancing.current && items.length > 0) {
      advancing.current = false;
    }
    if (items.length > 0 || dismissed.size === 0) return;
    if (advancing.current) return;
    const remaining = Math.max(0, queueTotal - dismissed.size);
    if (remaining <= 0) return;
    advancing.current = true;
    setPage(1);
    setDismissed(new Set());
    reloadQueue();
  }, [items.length, dismissed.size, queueTotal, queueLoading, reloadQueue]);

  const restore = useCallback((id: number) => {
    setDismissed((current) => {
      if (!current.has(id)) return current;
      const next = new Set(current);
      next.delete(id);
      return next;
    });
  }, []);

  const dismiss = useCallback((id: number) => {
    setDismissed((current) => {
      if (current.has(id)) return current;
      const next = new Set(current);
      next.add(id);
      return next;
    });
  }, []);

  const actionRunRef = useRef(action.run);
  actionRunRef.current = action.run;

  const decide = useCallback(
    (decision: "yes" | "no") => {
      const current = faceRef.current;
      if (!current) return;
      const id = current.id;
      if (pendingReviews.current.has(id)) return;
      pendingReviews.current.add(id);
      // Optimistic: leave the queue immediately so the next face is ready
      dismiss(id);
      void mutate(`/faces/${id}/review`, { decision })
        .then(() => {
          // Quiet success — toast on every click is noisy during rapid review. Undo with U / Ctrl+Z.
          pushUndo(`${decision === "yes" ? "Confirmed" : "Rejected"} face #${id}`, async () => {
            await mutate(`/faces/${id}/review`, { decision: "reset" });
            restore(id);
          }, { silent: true });
        })
        .catch((cause) => {
          // Roll back so the face returns to the queue
          setDismissed((prev) => {
            const next = new Set(prev);
            next.delete(id);
            return next;
          });
          const message =
            cause instanceof Error ? cause.message : String(cause);
          void actionRunRef.current(
            async () => {
              throw new Error(message);
            },
            undefined,
            false,
          );
        })
        .finally(() => {
          pendingReviews.current.delete(id);
        });
    },
    [dismiss, restore, pushUndo],
  );

  // Single keydown listener for the component lifetime — no per-render rebind.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (
        dialogRef.current ||
        viewerRef.current ||
        document.querySelector("dialog[open]")
      )
        return;
      if (
        event.target instanceof HTMLElement &&
        event.target.closest("input,textarea,select,[contenteditable=true]")
      )
        return;
      if (event.key.toLowerCase() === "u" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.repeat) {
        event.preventDefault();
        void undoLast();
        return;
      }
      const current = faceRef.current;
      if (
        !current ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        event.repeat
      )
        return;
      const key = event.key.toLowerCase();
      if (["y", "n", "d", "arrowright", "arrowleft", "v"].includes(key))
        event.preventDefault();
      if (key === "y") decide("yes");
      if (key === "n") decide("no");
      if (key === "d") setDialog("delete");
      if (key === "v") setViewer(current);
      if (key === "arrowright")
        setCursor((value) =>
          Math.min(itemsLenRef.current - 1, value + 1),
        );
      if (key === "arrowleft") setCursor((value) => Math.max(0, value - 1));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [decide, undoLast]);
  return (
    <>
      <PageHeader
        eyebrow="A HUMAN TOUCH MAKES THE DIFFERENCE"
        title="A familiar face?"
        description="Confirm the good matches. Correct the rest. Your decisions stay protected."
        actions={
          <Link className="button" to="/cleanup">
            <Icon name="cleanup" size={16} />
            Cleanup
          </Link>
        }
      />
      <section className="panel" aria-label="Local matching training" style={{ marginBottom: 16, padding: 16 }}>
        <div className="inline-actions" style={{ flexWrap: "wrap" }}>
          <strong>Learn from your reviews</strong>
          <Badge>{training.data?.active ? "Learned matching active" : "Cosine matching"}</Badge>
          <Button className="button small" disabled={action.busy || training.loading || !!training.error}
            onClick={() => void action.run(async () => {
              const report = await mutate<{ reason: string }>("/review/training");
              setTrainingMessage(report.reason);
              training.reload();
            }, undefined, false)}>Train matching model</Button>
          {training.data?.last_training && <Button className="button small" disabled={action.busy}
            onClick={() => void action.run(async () => {
              await mutate("/review/training", undefined, "DELETE");
              setTrainingMessage("Learned model reset. Your reviews and embeddings are preserved.");
              training.reload();
            }, undefined, false)}>Reset learned model</Button>}
        </div>
        <details className="small-text muted" style={{ marginTop: 8 }}>
          <summary>Training requirements</summary>
          <p>Only your scored Yes/No decisions train this local model. At least 100 reviews,
          including 30 matches and 30 non-matches, plus independent media are needed. Automatic training runs every 50 decisions.
          ArcFace stays unchanged; validation must pass before learned matching is used.</p>
        </details>
        {training.data && <p className="small-text">{number(training.data.positive_samples)} matches · {number(training.data.negative_samples)} non-matches
          {training.data.last_training?.validation && <> · Last holdout precision {percent(training.data.last_training.validation.precision)} · recall {percent(training.data.last_training.validation.recall)}</>}
        </p>}
        <p className="small-text" role="status">{trainingMessage || training.data?.state_note}</p>
        <ErrorNotice error={training.error || action.error} retry={training.reload} />
      </section>
      <div className="collection-filters">
        <div className="inline-actions">
          {personId ? (
            <PersonToken
              id={personId}
              onRemove={() => {
                setParams({});
                setPage(1);
              }}
            />
          ) : (
            <Badge>Everyone</Badge>
          )}
          <Button className="button small" onClick={() => setDialog("person")}>
            <Icon name="people" size={16} />
            Filter by person
          </Button>
        </div>
        {canUndo && <Button className="button small" onClick={() => void undoLast()}>Undo last decision</Button>}
        <span className="muted">
          {resource.data
            ? `${number(Math.max(0, resource.data.total - dismissed.size))} awaiting review`
            : "Loading queue"}
        </span>
      </div>
      <ErrorNotice error={resource.error} retry={resource.reload} />
      {resource.loading && !resource.data ? (
        <Loading label="Preparing your review queue" />
      ) : !face && resource.data ? (
        <Empty
          icon="review"
          title={
            resource.data.total
              ? "This page is reviewed"
              : "A little peace of mind"
          }
          description={
            resource.data.total
              ? "Continue to another page or refresh the queue to pick up the remaining faces."
              : "There are no faces waiting for review in this view. New uncertain matches will appear after indexing."
          }
        >
          <Button
            className="button"
            onClick={() => {
              setPage(1);
              setDismissed(new Set());
              resource.reload();
            }}
          >
            Refresh queue
          </Button>
          <Link className="button primary" to="/people">
            Explore people
            <Icon name="arrow" size={16} />
          </Link>
        </Empty>
      ) : (
        face && (
          <>
            <section className="review-workbench">
              <div className="review-image-side">
                <div className="review-image-label">
                  <Badge tone="amber">{face.confidence_label}</Badge>
                  <span>Face #{face.id}</span>
                </div>
                <Button
                  className="review-main-image image-button"
                  onClick={() => setViewer(face)}
                  aria-label="Open source media for this face"
                >
                  <Thumbnail
                    src={`/api/faces/${face.id}/thumbnail`}
                    alt={`Face suggested as ${face.display_name}`}
                    icon="people"
                  />
                </Button>
                <Button
                  className="review-source-link"
                  onClick={() => setViewer(face)}
                >
                  <Icon
                    name={face.timestamp === null ? "photo" : "video"}
                    size={16}
                  />
                  View original
                  {face.timestamp !== null
                    ? ` at ${timeLabel(face.timestamp)}`
                    : ""}
                  <kbd>V</kbd>
                </Button>
              </div>
              <div className="review-decision">
                <span className="eyebrow">LET'S PUT A NAME TO THE FACE</span>
                <h2>
                  Is this
                  <br />
                  <em>{face.display_name || "this person"}?</em>
                </h2>
                {face.person_id && (
                  <Link className="text-link" to={`/people/${face.person_id}`}>
                    Compare with their profile
                    <Icon name="arrow" size={14} />
                  </Link>
                )}
                <dl className="review-confidence">
                  <div>
                    <dt>Identity match</dt>
                    <dd>{percent(face.similarity)}</dd>
                  </div>
                  <div>
                    <dt>Face detection</dt>
                    <dd>{percent(face.detection)}</dd>
                  </div>
                </dl>
                <p className="muted small-text">
                  Confidence is a model estimate, not a guarantee. Trust what
                  you recognize.
                </p>
                <div className="review-primary-actions">
                  <Button
                    className="button primary"
                    onClick={() => decide("yes")}
                  >
                    <Icon name="check" />
                    Yes, that's them<kbd>Y</kbd>
                  </Button>
                  <Button
                    className="button"
                    onClick={() => decide("no")}
                  >
                    <Icon name="close" />
                    No, not them<kbd>N</kbd>
                  </Button>
                </div>
                <div className="review-secondary-actions">
                  <Button
                    className="text-link"
                    disabled={action.busy}
                    onClick={() => setDialog("move")}
                  >
                    Assign to someone else
                  </Button>
                  <Button
                    className="text-link danger-text"
                    disabled={action.busy}
                    onClick={() => setDialog("delete")}
                  >
                    <Icon name="trash" size={15} />
                    Delete face<kbd>D</kbd>
                  </Button>
                </div>
                <p className="small-text muted">
                  Deleting removes only the indexed face, never the original.
                </p>
                <div className="kbd-hints" aria-label="Keyboard shortcuts">
                  <span><kbd>Y</kbd> yes</span><span><kbd>N</kbd> no</span><span><kbd>D</kbd> delete</span>
                  <span><kbd>V</kbd> view</span><span><kbd>←</kbd><kbd>→</kbd> move</span><span><kbd>U</kbd> undo</span>
                </div>
              </div>
            </section>
            <div className="review-navigation">
              <Button
                className="button small"
                disabled={cursor <= 0 || action.busy}
                onClick={() => setCursor((value) => Math.max(0, value - 1))}
              >
                <Icon name="back" size={16} />
                Previous
              </Button>
              <span>
                {Math.min(cursor + 1, items.length)} of {items.length} on this
                page
              </span>
              <Button
                className="button small"
                disabled={cursor >= items.length - 1 || action.busy}
                onClick={() =>
                  setCursor((value) => Math.min(items.length - 1, value + 1))
                }
              >
                Skip for now
                <Icon name="arrow" size={16} />
              </Button>
            </div>
            <div className="review-filmstrip review-grid" aria-label="Faces on this page">
              {animatedItems.map(({ item, key, phase }, index) => (
                <Button
                  key={key}
                  className={`anim-item anim-${phase}`}
                  aria-label={`Review face ${item.id}: ${item.display_name}`}
                  aria-pressed={item.id === face?.id}
                  onClick={() => setCursor(index)}
                  disabled={action.busy}
                >
                  <Thumbnail
                    src={`/api/faces/${item.id}/thumbnail`}
                    alt=""
                    icon="people"
                  />
                </Button>
              ))}
            </div>
          </>
        )
      )}
      {resource.data && (
        <Pagination
          page={page}
          total={Math.max(0, resource.data.total - dismissed.size)}
          limit={PAGE_LIMIT}
          onPage={setPage}
        />
      )}
      {viewer && (
        <MediaViewer
          id={viewer.media_id}
          timestamp={viewer.timestamp}
          onClose={() => setViewer(null)}
        />
      )}
      <Dialog
        open={dialog === "person"}
        onClose={() => setDialog(null)}
        title="Review one person"
      >
        <div className="dialog-body">
          <PersonPicker
            onSelect={(person) => {
              setParams({ person_id: String(person.id) });
              setPage(1);
              setDialog(null);
            }}
          />
        </div>
      </Dialog>
      {dialog === "move" && face && (
        <MoveFacesDialog
          open
          faces={[face]}
          onClose={() => setDialog(null)}
          onMoved={() => dismiss(face.id)}
        />
      )}
      <ConfirmDialog
        open={dialog === "delete" && !!face}
        onClose={() => setDialog(null)}
        title="Delete this face from the index?"
        description="The original stays untouched. Restore this face later in Cleanup, under Deleted faces."
        label="Delete face"
        onConfirm={async () => {
          if (face) {
            const id = face.id;
            await mutate(`/faces/${id}`, undefined, "DELETE");
            dismiss(id);
            pushUndo(`Deleted face #${id}`, async () => {
              await mutate(`/faces/${id}/restore`);
              restore(id);
            });
          }
        }}
        success="Face soft-deleted."
      />
    </>
  );
}
