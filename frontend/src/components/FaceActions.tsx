import { useState } from "react";
import { Link } from "react-router-dom";
import { batch, mutate, percent, timeLabel } from "../api";
import { useAction } from "../context";
import type { Face, Person } from "../types";
import { Badge, ConfirmDialog, Dialog, ErrorNotice, Thumbnail } from "./ui";
import { Icon } from "./Icon";
import { PersonPicker } from "./PersonPicker";

export function FaceTile({
  face,
  selected,
  onSelect,
  onOpen,
}: {
  face: Face;
  selected?: boolean;
  onSelect?: () => void;
  onOpen: () => void;
}) {
  return (
    <article className={`face-tile ${selected ? "selected" : ""}`}>
      <div className="face-image">
        <button
          className="image-button"
          onClick={onOpen}
          aria-label={`Open source for ${face.display_name}, face ${face.id}`}
        >
          <Thumbnail
            src={`/api/faces/${face.id}/thumbnail`}
            alt={face.display_name}
            icon="people"
          />
        </button>
        {onSelect && (
          <label className="select-check">
            <input
              type="checkbox"
              checked={!!selected}
              onChange={onSelect}
              aria-label={`Select face ${face.id} of ${face.display_name}`}
            />
          </label>
        )}
        {face.timestamp !== null && (
          <span className="media-duration">{timeLabel(face.timestamp)}</span>
        )}
      </div>
      <div className="face-tile-info">
        <strong>
          {face.person_id ? (
            <Link to={`/people/${face.person_id}`}>{face.display_name}</Link>
          ) : (
            face.display_name || "Unassigned"
          )}
        </strong>
        <span className="small-text muted">
          Match {percent(face.similarity)}
        </span>
        <div className="badge-row">
          <Badge
            tone={
              face.deleted_at
                ? "red"
                : face.review_state === "confirmed"
                  ? "teal"
                  : "amber"
            }
          >
            {face.deleted_at ? "Deleted" : face.review_state}
          </Badge>
          {face.excluded && <Badge>Excluded</Badge>}
        </div>
      </div>
    </article>
  );
}

export function MoveFacesDialog({
  faces,
  open,
  onClose,
  onMoved,
}: {
  faces: Face[];
  open: boolean;
  onClose: () => void;
  onMoved?: (personId: number) => void;
}) {
  const [mode, setMode] = useState<"existing" | "new">("existing");
  const [target, setTarget] = useState<Person | null>(null);
  const [name, setName] = useState("");
  const action = useAction();
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Move ${faces.length} selected ${faces.length === 1 ? "face" : "faces"}`}
      busy={action.busy}
    >
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (mode === "existing" && !target) return;
          let personId = 0;
          const ok = await action.run(async () => {
            const result = await mutate<{ person_id: number }>("/faces/move", {
              face_ids: faces.map((face) => face.id),
              ...(mode === "existing"
                ? { target_id: target!.id }
                : { name: name.trim() }),
            });
            personId = result.person_id;
          }, `${faces.length} faces moved. Your correction is protected during reconciliation.`);
          if (ok) {
            onClose();
            onMoved?.(personId);
          }
        }}
      >
        <div className="dialog-body">
          <p>
            Correct an identity, or split these faces into their own person.
            Original photos and videos stay untouched.
          </p>
          <div className="segmented" role="group" aria-label="Move destination">
            <button
              type="button"
              aria-pressed={mode === "existing"}
              onClick={() => setMode("existing")}
            >
              Existing person
            </button>
            <button
              type="button"
              aria-pressed={mode === "new"}
              onClick={() => setMode("new")}
            >
              Split into new person
            </button>
          </div>
          {mode === "new" ? (
            <label className="field">
              New person's name <span className="muted">(optional)</span>
              <input
                value={name}
                maxLength={200}
                onChange={(event) => setName(event.target.value)}
                placeholder="Leave blank for an unnamed person"
              />
            </label>
          ) : (
            <>
              {target && (
                <div className="chosen-person">
                  <Icon name="people" />
                  <strong>{target.display_name}</strong>
                  <button
                    className="button ghost small"
                    type="button"
                    onClick={() => setTarget(null)}
                  >
                    Change
                  </button>
                </div>
              )}
              <PersonPicker onSelect={setTarget} label="Move faces to" />
            </>
          )}
          <ErrorNotice error={action.error} />
        </div>
        <div className="dialog-footer">
          <button
            className="button"
            type="button"
            onClick={onClose}
            disabled={action.busy}
          >
            Cancel
          </button>
          <button
            className="button primary"
            disabled={
              action.busy || !faces.length || (mode === "existing" && !target)
            }
          >
            {action.busy
              ? "Moving..."
              : mode === "new"
                ? "Create person & move"
                : "Move faces"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

export function FaceActions({
  faces,
  onDone,
  allowPermanent = false,
}: {
  faces: Face[];
  onDone?: () => void;
  allowPermanent?: boolean;
}) {
  const action = useAction();
  const [dialog, setDialog] = useState<"move" | "delete" | "permanent" | null>(
    null,
  );
  if (!faces.length) return null;
  const active = faces.filter((face) => !face.deleted_at);
  const deleted = faces.filter((face) => !!face.deleted_at);
  const pairs = [
    ...new Map(
      active
        .filter((face) => face.person_id !== null)
        .map((face) => [
          `${face.person_id}:${face.media_id}`,
          {
            person_id: face.person_id!,
            media_id: face.media_id,
            excluded: face.excluded,
          },
        ]),
    ).values(),
  ];
  const allExcluded = pairs.length > 0 && pairs.every((pair) => pair.excluded);
  const run = async (fn: () => Promise<unknown>, message: string) => {
    if (await action.run(fn, message)) onDone?.();
  };
  return (
    <div
      className="face-actions"
      aria-label="Selected face actions"
      aria-busy={action.busy}
    >
      {!!active.length && (
        <>
          <button
            className="button small"
            disabled={action.busy}
            onClick={() =>
              void run(
                () =>
                  batch(active, (face) =>
                    mutate(`/faces/${face.id}/review`, { decision: "yes" }),
                  ),
                "Faces confirmed.",
              )
            }
          >
            <Icon name="check" size={16} />
            Confirm
          </button>
          <button
            className="button small"
            disabled={action.busy}
            onClick={() =>
              void run(
                () =>
                  batch(active, (face) =>
                    mutate(`/faces/${face.id}/review`, { decision: "no" }),
                  ),
                "Incorrect matches rejected.",
              )
            }
          >
            <Icon name="close" size={16} />
            Not them
          </button>
          <button
            className="button small"
            disabled={action.busy}
            onClick={() => setDialog("move")}
          >
            <Icon name="merge" size={16} />
            Move / split
          </button>
          {!!pairs.length && (
            <button
              className="button small"
              disabled={action.busy}
              onClick={() =>
                void run(
                  () =>
                    batch(
                      pairs.filter((pair) => allExcluded || !pair.excluded),
                      (pair) =>
                        mutate(
                          "/exclusions",
                          {
                            person_id: pair.person_id,
                            media_id: pair.media_id,
                          },
                          allExcluded ? "DELETE" : "POST",
                        ),
                    ),
                  allExcluded
                    ? "Person-media exclusions removed."
                    : "Person-media matches excluded from default results.",
                )
              }
            >
              <Icon name={allExcluded ? "eye" : "hidden"} size={16} />
              {allExcluded ? "Include" : "Exclude"}
            </button>
          )}
          <button
            className="button small danger-text"
            disabled={action.busy}
            onClick={() => setDialog("delete")}
          >
            <Icon name="trash" size={16} />
            Delete
          </button>
        </>
      )}
      {!!deleted.length && (
        <button
          className="button small"
          disabled={action.busy}
          onClick={() =>
            void run(
              () =>
                batch(deleted, (face) => mutate(`/faces/${face.id}/restore`)),
              "Deleted faces restored.",
            )
          }
        >
          <Icon name="restore" size={16} />
          Restore
        </button>
      )}
      {allowPermanent && !!deleted.length && (
        <button
          className="button small danger-text"
          disabled={action.busy}
          onClick={() => setDialog("permanent")}
        >
          Delete permanently
        </button>
      )}
      {dialog === "move" && (
        <MoveFacesDialog
          open
          faces={active}
          onClose={() => setDialog(null)}
          onMoved={onDone}
        />
      )}
      <ConfirmDialog
        open={dialog === "delete"}
        onClose={() => setDialog(null)}
        title={`Delete ${active.length} faces from the index?`}
        description="This only soft-deletes face records. Original files are never removed. Restore faces later from Cleanup."
        label="Delete faces"
        onConfirm={async () => {
          await batch(active, (face) =>
            mutate(`/faces/${face.id}`, undefined, "DELETE"),
          );
          onDone?.();
        }}
        success="Faces moved to deleted records."
      />
      <ConfirmDialog
        open={dialog === "permanent"}
        onClose={() => setDialog(null)}
        title={`Permanently delete ${deleted.length} faces?`}
        description="These face records and their corrections cannot be restored. Original photos and videos will not be deleted."
        confirmation="DELETE FACE"
        label="Delete permanently"
        onConfirm={async () => {
          await batch(deleted, (face) =>
            mutate(
              `/faces/${face.id}/permanent`,
              { confirm: "DELETE FACE" },
              "DELETE",
            ),
          );
          onDone?.();
        }}
        success="Face records permanently removed."
      />
    </div>
  );
}
