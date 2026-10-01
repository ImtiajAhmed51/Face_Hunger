import { useState } from "react";
import { mutate } from "../api";
import { useAction, useApp } from "../context";
import { useResource } from "../hooks";
import { undoAudit, type Audited } from "../audit";
import type { Person } from "../types";
import { Dialog, ErrorNotice, Thumbnail } from "./ui";
import { PersonPicker } from "./PersonPicker";

export function RenameDialog({
  person,
  onClose,
}: {
  person: Person;
  onClose: () => void;
}) {
  const [name, setName] = useState(person.name ?? "");
  const action = useAction();
  return (
    <Dialog
      open
      onClose={onClose}
      title="Give a familiar face a name"
      busy={action.busy}
    >
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (
            await action.run(
              () =>
                mutate(`/people/${person.id}`, { name: name.trim() }, "PATCH"),
              "Person renamed.",
            )
          )
            onClose();
        }}
      >
        <div className="dialog-body">
          <label className="field">
            Name
            <input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={200}
              placeholder="Enter a name"
            />
          </label>
          <p className="muted">
            Leave blank to return to an unnamed person. Names stay in your local
            library.
          </p>
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
          <button className="button primary" disabled={action.busy}>
            {action.busy ? "Saving..." : "Save name"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
interface MergePreview {
  similarity: number | null;
  confidence: "high" | "medium" | "low" | null;
  least_similar: { face_id: number; similarity: number }[];
  face_count: number;
}

export function MergeDialog({
  person,
  onClose,
  onMerged,
}: {
  person: Person;
  onClose: () => void;
  onMerged: (id: number) => void;
}) {
  const [target, setTarget] = useState<Person | null>(null);
  const action = useAction();
  const { pushUndo } = useApp();
  const preview = useResource<MergePreview>(target ? `/people/${person.id}/merge-preview?target_id=${target.id}` : null);
  return (
    <Dialog
      open
      onClose={onClose}
      title={`Merge ${person.display_name}`}
      busy={action.busy}
    >
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (!target) return;
          if (
            await action.run(
              async () => {
                const result = await mutate<Audited>(`/people/${person.id}/merge`, { target_id: target.id });
                pushUndo(`Merged ${person.display_name} into ${target.display_name}`, undoAudit(result.audit_id));
              },
            )
          ) {
            onClose();
            onMerged(target.id);
          }
        }}
      >
        <div className="dialog-body">
          <p>
            All faces from <strong>{person.display_name}</strong> will move to
            the person you choose. The destination's name is kept. You can undo
            the merge from the notification, with Ctrl/⌘+Z, or later in Health → Activity.
          </p>
          {target && (
            <div className="chosen-person">
              <span>Merge into</span>
              <strong>{target.display_name}</strong>
            </div>
          )}
          {target && preview.data && (
            <div className={`merge-confidence tone-${preview.data.confidence ?? "unknown"}`} role="status">
              <strong>
                {preview.data.similarity === null
                  ? "No face data to compare"
                  : `${preview.data.confidence === "high" ? "Very likely" : preview.data.confidence === "medium" ? "Possibly" : "Unlikely"} the same person · ${Math.round(preview.data.similarity * 100)}% similar`}
              </strong>
              {preview.data.least_similar.length > 0 && (
                <>
                  <span className="small-text muted">Least similar faces of {person.display_name}: check these before merging</span>
                  <div className="merge-check-faces">
                    {preview.data.least_similar.slice(0, 8).map((face) => (
                      <figure key={face.face_id}>
                        <Thumbnail src={`/api/faces/${face.face_id}/thumbnail`} alt="" icon="people" />
                        <figcaption>{Math.round(face.similarity * 100)}%</figcaption>
                      </figure>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}
          <PersonPicker
            onSelect={setTarget}
            excluded={[person.id]}
            label="Destination person"
          />
          <ErrorNotice error={action.error} />
        </div>
        <div className="dialog-footer">
          <button
            type="button"
            className="button"
            onClick={onClose}
            disabled={action.busy}
          >
            Cancel
          </button>
          <button className="button primary" disabled={!target || action.busy}>
            {action.busy ? "Merging..." : "Merge people"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
