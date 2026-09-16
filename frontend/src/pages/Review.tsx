import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { mutate, number, percent, queryString, timeLabel } from "../api";
import { useAction } from "../context";
import { useResource } from "../hooks";
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

export function Review() {
  const [params, setParams] = useSearchParams();
  const personId = Number(params.get("person_id")) || undefined;
  const [page, setPage] = useState(1);
  const [cursor, setCursor] = useState(0);
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());
  const resource = useResource<Page<Face>>(
    `/review?${queryString({ page, limit: 30, person_id: personId, deleted: false })}`,
  );
  const [viewer, setViewer] = useState<Face | null>(null);
  const [dialog, setDialog] = useState<"delete" | "move" | "person" | null>(
    null,
  );
  const action = useAction();
  const items =
    resource.data?.items.filter((face) => !dismissed.has(face.id)) ?? [];
  const face = items[Math.min(cursor, Math.max(0, items.length - 1))];
  useEffect(() => {
    setCursor(0);
    setDismissed(new Set());
  }, [page, personId]);
  useEffect(() => {
    setCursor((value) => Math.min(value, Math.max(0, items.length - 1)));
  }, [items.length]);
  const dismiss = (id: number) =>
    setDismissed((current) => new Set([...current, id]));
  const decide = async (decision: "yes" | "no") => {
    if (!face || action.busy) return;
    const id = face.id;
    // Optimistic: advance UI immediately; don't wait on full collection refresh.
    dismiss(id);
    const ok = await action.run(
      () => mutate(`/faces/${id}/review`, { decision }),
      decision === "yes" ? "Match confirmed." : "Incorrect match rejected.",
      false, // skip global lfs:refresh — keeps Yes/No snappy
    );
    if (!ok) {
      // Roll back dismiss on failure so the face stays in the queue
      setDismissed((current) => {
        const next = new Set(current);
        next.delete(id);
        return next;
      });
    }
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (
        dialog ||
        viewer ||
        action.busy ||
        document.querySelector("dialog[open]")
      )
        return;
      if (
        event.target instanceof HTMLElement &&
        event.target.closest("input,textarea,select,[contenteditable=true]")
      )
        return;
      if (
        !face ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        event.repeat
      )
        return;
      const key = event.key.toLowerCase();
      if (["y", "n", "d", "arrowright", "arrowleft", "v"].includes(key))
        event.preventDefault();
      if (key === "y") void decide("yes");
      if (key === "n") void decide("no");
      if (key === "d") setDialog("delete");
      if (key === "v") setViewer(face);
      if (key === "arrowright")
        setCursor((value) => Math.min(items.length - 1, value + 1));
      if (key === "arrowleft") setCursor((value) => Math.max(0, value - 1));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });
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
          <button className="button small" onClick={() => setDialog("person")}>
            <Icon name="people" size={16} />
            Filter by person
          </button>
        </div>
        <span className="muted">
          {resource.data
            ? `${number(resource.data.total)} awaiting review`
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
          <button
            className="button"
            onClick={() => {
              setPage(1);
              setDismissed(new Set());
              resource.reload();
            }}
          >
            Refresh queue
          </button>
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
                <button
                  className="review-main-image image-button"
                  onClick={() => setViewer(face)}
                  aria-label="Open source media for this face"
                >
                  <Thumbnail
                    src={`/api/faces/${face.id}/thumbnail`}
                    alt={`Face suggested as ${face.display_name}`}
                    icon="people"
                  />
                </button>
                <button
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
                </button>
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
                  <button
                    className="button primary"
                    disabled={action.busy}
                    onClick={() => void decide("yes")}
                  >
                    <Icon name="check" />
                    Yes, that's them<kbd>Y</kbd>
                  </button>
                  <button
                    className="button"
                    disabled={action.busy}
                    onClick={() => void decide("no")}
                  >
                    <Icon name="close" />
                    No, not them<kbd>N</kbd>
                  </button>
                </div>
                <div className="review-secondary-actions">
                  <button
                    className="text-link"
                    disabled={action.busy}
                    onClick={() => setDialog("move")}
                  >
                    Assign to someone else
                  </button>
                  <button
                    className="text-link danger-text"
                    disabled={action.busy}
                    onClick={() => setDialog("delete")}
                  >
                    <Icon name="trash" size={15} />
                    Delete face<kbd>D</kbd>
                  </button>
                </div>
                <p className="small-text muted">
                  Deleting removes only the indexed face, never the original.
                </p>
              </div>
            </section>
            <div className="review-navigation">
              <button
                className="button small"
                disabled={cursor <= 0 || action.busy}
                onClick={() => setCursor((value) => Math.max(0, value - 1))}
              >
                <Icon name="back" size={16} />
                Previous
              </button>
              <span>
                {Math.min(cursor + 1, items.length)} of {items.length} on this
                page
              </span>
              <button
                className="button small"
                disabled={cursor >= items.length - 1 || action.busy}
                onClick={() =>
                  setCursor((value) => Math.min(items.length - 1, value + 1))
                }
              >
                Skip for now
                <Icon name="arrow" size={16} />
              </button>
            </div>
            <div className="review-filmstrip" aria-label="Faces on this page">
              {items.map((item, index) => (
                <button
                  key={item.id}
                  aria-label={`Review face ${item.id}: ${item.display_name}`}
                  aria-pressed={item.id === face.id}
                  onClick={() => setCursor(index)}
                  disabled={action.busy}
                >
                  <Thumbnail
                    src={`/api/faces/${item.id}/thumbnail`}
                    alt=""
                    icon="people"
                  />
                </button>
              ))}
            </div>
          </>
        )
      )}
      {resource.data && (
        <Pagination
          page={page}
          total={resource.data.total}
          limit={30}
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
            await mutate(`/faces/${face.id}`, undefined, "DELETE");
            dismiss(face.id);
          }
        }}
        success="Face soft-deleted."
      />
    </>
  );
}
