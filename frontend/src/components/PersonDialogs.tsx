import { useState } from "react";
import { mutate } from "../api";
import { useAction } from "../context";
import type { Person } from "../types";
import { Dialog, ErrorNotice } from "./ui";
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
              () =>
                mutate(`/people/${person.id}/merge`, { target_id: target.id }),
              `Merged into ${target.display_name}.`,
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
            the person you choose. The destination's name is kept. To undo an
            incorrect merge, select the affected faces and split them into a new
            person.
          </p>
          {target && (
            <div className="chosen-person">
              <span>Merge into</span>
              <strong>{target.display_name}</strong>
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
