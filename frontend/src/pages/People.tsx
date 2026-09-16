import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { exportZip, mutate, number, queryString } from "../api";
import { useAction, useApp } from "../context";
import { useDebounced, useResource } from "../hooks";
import type { Page, Person } from "../types";
import { Icon } from "../components/Icon";
import { MediaCollection } from "../components/MediaGrid";
import { MergeDialog, RenameDialog } from "../components/PersonDialogs";
import { PersonFaces } from "../components/PersonFaces";
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

const PEOPLE_SORT_OPTIONS = [
  { value: "faces", label: "Most faces" },
  { value: "photos", label: "Most photos" },
  { value: "videos", label: "Most videos" },
  { value: "name", label: "Name A–Z" },
] as const;

export function People() {
  const [q, setQ] = useState("");
  const query = useDebounced(q);
  const [page, setPage] = useState(1);
  const [sort, setSort] =
    useState<(typeof PEOPLE_SORT_OPTIONS)[number]["value"]>("faces");
  const resource = useResource<Page<Person>>(
    `/people?${queryString({ q: query, page, limit: 48, sort })}`,
  );
  return (
    <>
      <PageHeader
        eyebrow="THE PEOPLE MAKE THE PICTURE"
        title="Familiar faces"
        description="A collection of connections. Give a face a name, and find their moments."
      />
      <div className="collection-filters">
        <label className="input-icon">
          <Icon name="search" size={18} />
          <input
            type="search"
            aria-label="Search people by name"
            value={q}
            onChange={(event) => {
              setQ(event.target.value);
              setPage(1);
            }}
            placeholder="Find someone..."
          />
        </label>
        <label className="field-inline">
          <span className="sr-only">Sort people</span>
          <select
            aria-label="Sort people"
            value={sort}
            onChange={(event) => {
              setSort(event.target.value as typeof sort);
              setPage(1);
            }}
          >
            {PEOPLE_SORT_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
        <span className="muted">
          {resource.data
            ? `${number(resource.data.total)} people`
            : "Local face groups"}
        </span>
      </div>
      <ErrorNotice error={resource.error} retry={resource.reload} />
      {resource.loading && !resource.data ? (
        <Loading label="Finding familiar faces" />
      ) : resource.data && !resource.data.items.length ? (
        <Empty
          icon="people"
          title={q ? "No one by that name" : "Every face has a story"}
          description={
            q
              ? "Try another name, or browse the unnamed faces in your collection."
              : "Connect a folder and run your first scan. The people in your photos and videos will find a home here."
          }
        >
          {q ? (
            <button
              className="button"
              onClick={() => {
                setQ("");
                setPage(1);
              }}
            >
              Clear search
            </button>
          ) : (
            <Link to="/settings" className="button primary">
              <Icon name="folder" size={16} />
              Connect a library
            </Link>
          )}
        </Empty>
      ) : (
        <div className="people-grid">
          {resource.data?.items.map((person) => (
            <Link
              className="person-card"
              key={person.id}
              to={`/people/${person.id}`}
            >
              <div className="person-portrait">
                <Thumbnail
                  src={
                    person.representative_face_id
                      ? `/api/faces/${person.representative_face_id}/thumbnail`
                      : null
                  }
                  alt={person.display_name}
                  icon="people"
                />
                {!!person.unreviewed_count && (
                  <Badge tone="amber">
                    {person.unreviewed_count} to review
                  </Badge>
                )}
              </div>
              <div className="person-caption">
                <h2>{person.display_name}</h2>
                <span>{number(person.face_count)} faces</span>
                <div>
                  <span>
                    <Icon name="photo" size={14} />
                    {number(person.photo_count)}
                  </span>
                  <span>
                    <Icon name="video" size={14} />
                    {number(person.video_count)}
                  </span>
                  <Icon name="arrow" size={16} />
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
      {resource.data && (
        <Pagination
          page={page}
          limit={48}
          total={resource.data.total}
          onPage={setPage}
        />
      )}
    </>
  );
}

function MoveMediaDialog({
  person,
  onClose,
  onDone,
}: {
  person: Person;
  onClose: () => void;
  onDone: () => void;
}) {
  const action = useAction();
  const { notify } = useApp();
  const [destination, setDestination] = useState("");
  return (
    <Dialog
      open
      onClose={onClose}
      title={`Move media of ${person.display_name}`}
      busy={action.busy}
    >
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          const dest = destination.trim();
          if (!dest) return;
          const ok = await action.run(async () => {
            const result = await mutate<{
              moved: number;
              failed: { path: string; error: string }[];
              destination: string;
              removed_from_index: number;
            }>(`/people/${person.id}/move-media`, { destination: dest });
            notify(
              `Moved ${result.moved} file(s) to ${result.destination}. Removed ${result.removed_from_index} from the index.${
                result.failed?.length ? ` ${result.failed.length} failed.` : ""
              }`,
            );
          });
          if (ok) {
            onDone();
            onClose();
          }
        }}
      >
        <div className="dialog-body">
          <p>
            Original photos and videos that contain this person will be{" "}
            <strong>physically moved</strong> to the folder you choose. After
            the move they are removed from the face index and database — only
            media that was not moved stays searchable. Clustering for any
            remaining faces of this person is kept.
          </p>
          <label className="field">
            Destination folder path
            <input
              autoFocus
              value={destination}
              onChange={(e) => setDestination(e.target.value)}
              placeholder="/path/to/your/folder"
              autoComplete="off"
              spellCheck={false}
              required
            />
          </label>
          <p className="muted small-text">
            The folder will be created if it does not exist. Path must be under
            your allowed library roots.
          </p>
          <ErrorNotice error={action.error} />
        </div>
        <div className="dialog-footer">
          <button
            type="button"
            className="button"
            disabled={action.busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            className="button primary"
            disabled={action.busy || !destination.trim()}
          >
            {action.busy ? "Moving..." : "Move media & remove from index"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

export function PersonProfile() {
  const { id } = useParams();
  const personId = Number(id);
  const resource = useResource<Person>(
    Number.isSafeInteger(personId) && personId > 0
      ? `/people/${personId}`
      : null,
  );
  const navigate = useNavigate();
  const action = useAction();
  const [dialog, setDialog] = useState<
    "rename" | "merge" | "delete" | "restore" | "move" | null
  >(null);
  const [tab, setTab] = useState<"all" | "photo" | "video" | "faces">("all");
  const person = resource.data;
  if (!Number.isSafeInteger(personId) || personId <= 0)
    return (
      <Empty
        icon="people"
        title="Person not found"
        description="This profile link is not valid."
      >
        <Link className="button" to="/people">
          Browse people
        </Link>
      </Empty>
    );
  return (
    <>
      <Link className="back-link" to="/people">
        <Icon name="back" size={16} />
        All people
      </Link>
      <ErrorNotice error={resource.error} retry={resource.reload} />
      {resource.loading && !person && <Loading label="Opening profile" />}
      {person && (
        <>
          <header className="profile-header">
            <Thumbnail
              className="profile-portrait"
              src={
                person.representative_face_id
                  ? `/api/faces/${person.representative_face_id}/thumbnail`
                  : null
              }
              alt={person.display_name}
              icon="people"
            />
            <div className="profile-copy">
              <p className="eyebrow">A FACE IN YOUR COLLECTION</p>
              <h1>{person.display_name}</h1>
              <p>
                {number(person.face_count)} faces <span>·</span>{" "}
                {number(person.photo_count)} photos <span>·</span>{" "}
                {number(person.video_count)} videos
              </p>
              {!!person.unreviewed_count && (
                <Link
                  className="text-link"
                  to={`/review?person_id=${person.id}`}
                >
                  {number(person.unreviewed_count)} faces to review
                  <Icon name="arrow" size={14} />
                </Link>
              )}
            </div>
            <div className="profile-actions">
              <button className="button" onClick={() => setDialog("rename")}>
                <Icon name="edit" size={16} />
                Rename
              </button>
              <button className="button" onClick={() => setDialog("merge")}>
                <Icon name="merge" size={16} />
                Merge
              </button>
              <button
                className="button primary"
                disabled={action.busy}
                onClick={() =>
                  void action.run(
                    () => exportZip(`/people/${person.id}/export`),
                    "Person export downloaded.",
                    false,
                  )
                }
              >
                <Icon name="download" size={16} />
                {action.busy ? "Exporting..." : "Export"}
              </button>
              <details className="actions-menu">
                <summary className="button">More actions</summary>
                <div>
                  <button onClick={() => setDialog("move")}>
                    <Icon name="folder" size={16} />
                    Move media to folder…
                  </button>
                  <button onClick={() => setDialog("restore")}>
                    <Icon name="restore" size={16} />
                    Restore deleted faces
                  </button>
                  <button
                    className="danger-text"
                    onClick={() => setDialog("delete")}
                  >
                    <Icon name="trash" size={16} />
                    Delete person's faces
                  </button>
                </div>
              </details>
            </div>
          </header>
          <div className="tabs" role="group" aria-label="Profile view">
            {(
              [
                ["all", "All moments"],
                ["photo", "Photos"],
                ["video", "Videos"],
                ["faces", "Individual faces"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                aria-pressed={tab === value}
                onClick={() => setTab(value)}
              >
                {label}
              </button>
            ))}
          </div>
          {tab === "faces" ? (
            <PersonFaces key={personId} personId={personId} />
          ) : (
            <MediaCollection
              key={`${personId}:${tab}`}
              filters={{
                people: [person.id],
                ...(tab === "all" ? {} : { kind: tab }),
              }}
              personId={person.id}
              emptyTitle="No moments in this view"
              emptyDescription="Try another view, include excluded matches, or restore deleted media. Individual faces can be corrected in the Faces tab."
            />
          )}
          {dialog === "rename" && (
            <RenameDialog person={person} onClose={() => setDialog(null)} />
          )}
          {dialog === "merge" && (
            <MergeDialog
              person={person}
              onClose={() => setDialog(null)}
              onMerged={(target) => navigate(`/people/${target}`)}
            />
          )}
          {dialog === "move" && (
            <MoveMediaDialog
              person={person}
              onClose={() => setDialog(null)}
              onDone={() => resource.reload()}
            />
          )}
          <ConfirmDialog
            open={dialog === "delete"}
            onClose={() => setDialog(null)}
            title={`Delete faces of ${person.display_name}?`}
            description="All faces assigned to this person will be soft-deleted. The person and original files stay intact. Use More actions to restore the deleted faces."
            label="Delete faces"
            onConfirm={() =>
              mutate(`/people/${person.id}`, undefined, "DELETE")
            }
            success="Person's faces soft-deleted. Originals are untouched."
          />
          <ConfirmDialog
            open={dialog === "restore"}
            onClose={() => setDialog(null)}
            title={`Restore faces of ${person.display_name}?`}
            description="All soft-deleted faces assigned to this person will return to the active index. Permanently deleted face records cannot be restored."
            label="Restore faces"
            danger={false}
            onConfirm={() => mutate(`/people/${person.id}/restore`)}
            success="Person's deleted faces restored."
          />
        </>
      )}
    </>
  );
}
