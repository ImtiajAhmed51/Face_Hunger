import { useState } from "react";
import { Link } from "react-router-dom";
import { batch, mutate, number, percent } from "../api";
import { useAction, useApp } from "../context";
import { useResource, useSelection } from "../hooks";
import type { Cleanup as CleanupData, Library, Media, Person } from "../types";
import { Icon } from "../components/Icon";
import { MediaCollection, MediaGrid } from "../components/MediaGrid";
import { MediaViewer } from "../components/MediaViewer";
import { DeletedFaces } from "../components/PersonFaces";
import {
  Badge,
  ConfirmDialog,
  Dialog,
  Empty,
  ErrorNotice,
  Loading,
  PageHeader,
  Thumbnail,
} from "../components/ui";

function DuplicatePair({
  a,
  b,
  similarity,
  onSkipped,
}: {
  a: Person;
  b: Person;
  similarity: number;
  onSkipped?: () => void;
}) {
  const [target, setTarget] = useState(b.id);
  const [merge, setMerge] = useState(false);
  const [gone, setGone] = useState(false);
  const action = useAction();
  const source = target === a.id ? b : a;
  const destination = target === a.id ? a : b;
  if (gone) return null;
  return (
    <article className="duplicate-card">
      <div className="duplicate-people">
        {[a, b].map((person) => (
          <Link to={`/people/${person.id}`} key={person.id}>
            <Thumbnail
              src={
                person.representative_face_id
                  ? `/api/faces/${person.representative_face_id}/thumbnail`
                  : null
              }
              alt={person.display_name}
              icon="people"
            />
            <strong>{person.display_name}</strong>
            <span>{number(person.face_count)} faces</span>
          </Link>
        ))}
        <span className="duplicate-link">
          <Icon name="merge" />
        </span>
      </div>
      <div className="duplicate-details">
        <Badge tone="amber">{percent(similarity)} similarity</Badge>
        <p>Could these be the same person?</p>
        <label className="field">
          Keep this profile when merging
          <select
            value={target}
            onChange={(event) => setTarget(Number(event.target.value))}
          >
            <option value={a.id}>
              {a.display_name} (#{a.id})
            </option>
            <option value={b.id}>
              {b.display_name} (#{b.id})
            </option>
          </select>
        </label>
        <div className="inline-actions">
          <button
            className="button primary small"
            disabled={action.busy}
            onClick={() => setMerge(true)}
          >
            <Icon name="merge" size={16} />
            Merge
          </button>
          <button
            className="button small"
            disabled={action.busy}
            onClick={() =>
              void action.run(async () => {
                await mutate("/cleanup/separate", {
                  person_a: a.id,
                  person_b: b.id,
                });
                setGone(true);
                onSkipped?.();
              }, "Skipped — will not suggest this pair again. Fresh suggestions load next.")
            }
          >
            Skip / not the same
          </button>
        </div>
      </div>
      <ConfirmDialog
        open={merge}
        onClose={() => setMerge(false)}
        title="Merge these people?"
        description={`All faces in ${source.display_name} (#${source.id}) will move to ${destination.display_name} (#${destination.id}). The destination profile is kept. You can split individual faces later if needed.`}
        label="Merge people"
        danger={false}
        onConfirm={() =>
          mutate(`/people/${source.id}/merge`, { target_id: destination.id })
        }
        success="People merged."
      />
    </article>
  );
}

function IssueMedia({ items, missing }: { items: Media[]; missing?: boolean }) {
  const [viewer, setViewer] = useState<number | null>(null);
  const [remove, setRemove] = useState(false);
  const [retry, setRetry] = useState(false);
  const [library, setLibrary] = useState("");
  const libraries = useResource<{ items: Library[] }>(
    retry ? "/libraries" : null,
  );
  const selection = useSelection(items.map((item) => item.id).join(","));
  const action = useAction();
  const { job } = useApp();
  const active = !!job && ["running", "queued", "paused"].includes(job.status);
  return (
    <section>
      <div className="issue-help">
        <Icon name={missing ? "folder" : "alert"} />
        <p>
          {missing
            ? "Reconnect the drive or restore files to their original paths, then run Check library health. Removing a record only changes the local index."
            : "Load the local model, check file permissions, then retry failed files in the affected library. Open a file to inspect its metadata."}
        </p>
      </div>
      <div className="collection-bar">
        <label className="check-label">
          <input
            type="checkbox"
            disabled={!items.length}
            checked={
              !!items.length &&
              items.every((item) => selection.selected.has(item.id))
            }
            onChange={() => selection.all(items.map((item) => item.id))}
          />
          Select listed files
        </label>
        <div className="inline-actions">
          {!missing && (
            <button
              className="button small"
              disabled={active}
              title={
                active ? "Finish or cancel the active job first" : undefined
              }
              onClick={() => setRetry(true)}
            >
              <Icon name="restore" size={16} />
              Retry failed files
            </button>
          )}
          <button
            className="button small danger-text"
            disabled={!selection.selected.size}
            onClick={() => setRemove(true)}
          >
            Remove selected records
          </button>
        </div>
      </div>
      {items.length ? (
        <MediaGrid
          items={items}
          selected={selection.selected}
          onSelect={selection.toggle}
          onOpen={setViewer}
        />
      ) : (
        <Empty
          icon={missing ? "folder" : "check"}
          title={
            missing
              ? "Everything is where it belongs"
              : "No failed files listed"
          }
          description={
            missing
              ? "No missing files were reported. Run a health check after reconnecting drives or moving files."
              : "If a future scan encounters files it cannot process, they will appear here."
          }
        />
      )}
      {viewer !== null && (
        <MediaViewer
          id={viewer}
          ids={items.map((item) => item.id)}
          onClose={() => setViewer(null)}
        />
      )}
      <ConfirmDialog
        open={remove}
        onClose={() => setRemove(false)}
        title="Remove selected index records?"
        description="Originals are never removed. These media records will be soft-deleted and can be restored in Deleted records."
        label="Remove from index"
        onConfirm={async () => {
          await batch([...selection.selected], (id) =>
            mutate(`/media/${id}`, undefined, "DELETE"),
          );
          selection.clear();
        }}
        success="Selected index records removed."
      />
      <Dialog
        open={retry}
        onClose={() => setRetry(false)}
        title="Retry failed files"
        busy={action.busy}
      >
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            if (!library) return;
            if (
              await action.run(
                () =>
                  mutate("/index", {
                    library_id: Number(library),
                    retry_failed: true,
                  }),
                "Retry scan started. Track progress in Settings.",
              )
            )
              setRetry(false);
          }}
        >
          <div className="dialog-body">
            <p>
              Choose the library to retry. Only one indexing job can run at a
              time.
            </p>
            <ErrorNotice
              error={libraries.error || action.error}
              retry={libraries.reload}
            />
            {libraries.loading ? (
              <Loading label="Loading libraries" />
            ) : (
              <label className="field">
                Library
                <select
                  value={library}
                  onChange={(event) => setLibrary(event.target.value)}
                  required
                >
                  <option value="">Choose a library</option>
                  {libraries.data?.items.map((item) => (
                    <option value={item.id} key={item.id}>
                      {item.name} - {item.path}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <div className="dialog-footer">
            <button
              type="button"
              className="button"
              disabled={action.busy}
              onClick={() => setRetry(false)}
            >
              Cancel
            </button>
            <button
              className="button primary"
              disabled={action.busy || !library || active}
            >
              {action.busy ? "Starting..." : "Retry failed files"}
            </button>
          </div>
        </form>
      </Dialog>
    </section>
  );
}


function SoftOriginalsPanel({
  items,
  onChanged,
  onPurgeAll,
}: {
  items: {
    media_id: number;
    media_name: string | null;
    original_path: string;
    original_name: string;
    size: number;
  }[];
  onChanged: () => void;
  onPurgeAll: () => void;
}) {
  const action = useAction();
  const totalBytes = items.reduce((s, i) => s + (i.size || 0), 0);
  if (!items.length) {
    return (
      <Empty
        icon="photo"
        title="No kept originals"
        description="After a video is converted, the previous file is soft-kept as *.lfs_original. None are on disk right now."
      />
    );
  }
  return (
    <div className="soft-originals-panel">
      <div className="inline-actions" style={{ marginBottom: "1rem", flexWrap: "wrap", gap: "0.5rem" }}>
        <p className="muted small-text" style={{ flex: 1, margin: 0, minWidth: "12rem" }}>
          {items.length} soft-kept file{items.length === 1 ? "" : "s"} ·{" "}
          {(totalBytes / (1024 * 1024)).toFixed(1)} MB on disk. Restore puts the
          original back in place (converted MP4 is kept as *.lfs_converted backup).
        </p>
        <button
          className="button"
          disabled={action.busy}
          onClick={() =>
            void action.run(async () => {
              const result = await mutate<{ restored: number; failed: unknown[] }>(
                "/media/soft-originals/restore-all",
                {},
              );
              onChanged();
              const failed = result?.failed?.length ?? 0;
              if (failed) {
                throw new Error(
                  `Restored ${result?.restored ?? 0}; ${failed} failed. Check paths and retry.`,
                );
              }
            }, "All originals restored to their previous paths.")
          }
        >
          <Icon name="restore" size={16} />
          Restore all
        </button>
        <button className="button danger" onClick={onPurgeAll} disabled={action.busy}>
          <Icon name="trash" size={16} />
          Delete all permanently
        </button>
      </div>
      <div className="soft-originals-list">
        {items.map((item) => (
          <article key={`${item.media_id}:${item.original_path}`} className="soft-original-row">
            <div className="soft-original-info">
              <strong title={item.original_path}>{item.original_name}</strong>
              <span className="muted small-text">
                Media #{item.media_id}
                {item.media_name ? ` · ${item.media_name}` : ""} ·{" "}
                {(item.size / (1024 * 1024)).toFixed(1)} MB
              </span>
              <span className="muted small-text" title={item.original_path}>
                {item.original_path}
              </span>
            </div>
            <div className="inline-actions">
              <button
                className="button small"
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await mutate(`/media/${item.media_id}/soft-original/restore`, {
                      original_path: item.original_path,
                    });
                    onChanged();
                  }, "Original restored; converted file kept as backup.")
                }
              >
                <Icon name="restore" size={14} />
                Restore
              </button>
              <a className="button small" href={`/api/media/${item.media_id}/original`} download>
                Download
              </a>
              <button
                className="button small danger"
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await mutate(`/media/${item.media_id}/soft-original`, undefined, "DELETE");
                    onChanged();
                  }, "Original permanently deleted.")
                }
              >
                Delete forever
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}



function ConvertedBackupsPanel({
  items,
  onChanged,
  onPurgeAll,
}: {
  items: {
    path: string;
    name: string;
    size: number;
    media_id: number | null;
    media_name: string | null;
  }[];
  onChanged: () => void;
  onPurgeAll: () => void;
}) {
  const action = useAction();
  const totalBytes = items.reduce((s, i) => s + (i.size || 0), 0);
  if (!items.length) {
    return (
      <Empty
        icon="photo"
        title="No conversion backups"
        description="When you restore a kept original, the converted MP4 is renamed to *.lfs_converted. None are on disk right now."
      />
    );
  }
  return (
    <div className="soft-originals-panel">
      <div className="inline-actions" style={{ marginBottom: "1rem", flexWrap: "wrap", gap: "0.5rem" }}>
        <p className="muted small-text" style={{ flex: 1, margin: 0, minWidth: "12rem" }}>
          {items.length} converted backup{items.length === 1 ? "" : "s"} ·{" "}
          {(totalBytes / (1024 * 1024)).toFixed(1)} MB. These are leftover files from
          restore (safe to delete if the restored original is fine).
        </p>
        <button className="button danger" onClick={onPurgeAll} disabled={action.busy}>
          <Icon name="trash" size={16} />
          Delete all permanently
        </button>
      </div>
      <div className="soft-originals-list">
        {items.map((item) => (
          <article key={item.path} className="soft-original-row">
            <div className="soft-original-info">
              <strong title={item.path}>{item.name}</strong>
              <span className="muted small-text">
                {item.media_id != null ? `Media #${item.media_id}` : "Unlinked"}
                {item.media_name ? ` · ${item.media_name}` : ""} ·{" "}
                {(item.size / (1024 * 1024)).toFixed(1)} MB
              </span>
              <span className="muted small-text" title={item.path}>
                {item.path}
              </span>
            </div>
            <div className="inline-actions">
              <button
                className="button small danger"
                disabled={action.busy}
                onClick={() =>
                  void action.run(async () => {
                    await mutate("/media/converted-backups/delete", { path: item.path });
                    onChanged();
                  }, "Converted backup deleted.")
                }
              >
                Delete forever
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}


export function Cleanup() {
  const resource = useResource<CleanupData>("/cleanup");
  const [tab, setTab] = useState<
    "duplicates" | "failed" | "missing" | "deleted" | "originals" | "converted"
  >("duplicates");
  const [purgeOriginalsOpen, setPurgeOriginalsOpen] = useState(false);
  const [purgeConvertedOpen, setPurgeConvertedOpen] = useState(false);
  const [deletedTab, setDeletedTab] = useState<"faces" | "media">("faces");
  const action = useAction();
  const data = resource.data;
  return (
    <>
      <PageHeader
        eyebrow="A LITTLE CARE GOES A LONG WAY"
        title="Room to remember"
        description="Bring duplicate people together. Find missing files. Keep your collection healthy. Media duplicates live in the Duplicates tab."
        actions={
          <button
            className="button"
            disabled={action.busy}
            onClick={() =>
              void action.run(
                () => mutate("/cleanup/check"),
                "Library health check completed.",
              )
            }
          >
            <Icon name="shield" size={17} />
            {action.busy ? "Checking..." : "Check library health"}
          </button>
        }
      />
      <ErrorNotice error={resource.error} retry={resource.reload} />
      <ConfirmDialog
        open={purgeOriginalsOpen}
        onClose={() => setPurgeOriginalsOpen(false)}
        title="Delete all kept originals?"
        description="This permanently deletes every soft-kept pre-conversion file (*.lfs_original) from disk. Converted MP4 files stay. This cannot be undone."
        label="Delete all forever"
        confirmation="DELETE"
        onConfirm={async () => {
          await mutate("/media/soft-originals/purge", {});
          resource.reload();
        }}
        success="All kept originals permanently deleted."
      />
      <ConfirmDialog
        open={purgeConvertedOpen}
        onClose={() => setPurgeConvertedOpen(false)}
        title="Delete all conversion backups?"
        description="This permanently deletes every *.lfs_converted backup from disk. Restored originals and current library files stay. This cannot be undone."
        label="Delete all forever"
        confirmation="DELETE"
        onConfirm={async () => {
          await mutate("/media/converted-backups/purge", {});
          resource.reload();
        }}
        success="All conversion backups permanently deleted."
      />
      {resource.loading && !data && (
        <Loading label="Checking your collection" />
      )}
      {data && (
        <>
          <div className="cleanup-summary">
            <button onClick={() => setTab("duplicates")}>
              <span>Duplicate candidates</span>
              <strong>{number(data.duplicates)}</strong>
            </button>
            <Link to="/duplicates">
              <span>Media duplicates</span>
              <strong>→</strong>
            </Link>
            <Link to="/review">
              <span>Low confidence</span>
              <strong>{number(data.low_confidence)}</strong>
            </Link>
            <Link to="/review">
              <span>Unreviewed faces</span>
              <strong>{number(data.unreviewed)}</strong>
            </Link>
            <button onClick={() => setTab("failed")}>
              <span>Failed files</span>
              <strong>{number(data.failed)}</strong>
            </button>
            <button onClick={() => setTab("missing")}>
              <span>Missing files</span>
              <strong>{number(data.missing)}</strong>
            </button>
            <button onClick={() => setTab("deleted")}>
              <span>Deleted faces</span>
              <strong>{number(data.deleted_faces)}</strong>
            </button>
            <button onClick={() => setTab("originals")}>
              <span>Kept originals</span>
              <strong>
                {number(
                  Array.isArray(data.soft_originals)
                    ? data.soft_originals.length
                    : data.soft_originals || 0,
                )}
              </strong>
            </button>
            <button onClick={() => setTab("converted")}>
              <span>Conversion backups</span>
              <strong>
                {number(
                  Array.isArray(data.converted_backups)
                    ? data.converted_backups.length
                    : data.converted_backups || 0,
                )}
              </strong>
            </button>
          </div>
          {data.embedding_errors !== null && (
            <div
              className={`health-line ${data.embedding_errors ? "warning" : ""}`}
            >
              <Icon
                name={data.embedding_errors ? "alert" : "check"}
                size={18}
              />
              <span>
                {data.embedding_errors
                  ? `${number(data.embedding_errors)} embedding errors found. Rebuild the AI index from Settings after checking your libraries.`
                  : "Embedding integrity check: no errors reported."}
              </span>
              {!!data.embedding_errors && (
                <Link className="text-link" to="/settings">
                  Maintenance
                  <Icon name="arrow" size={14} />
                </Link>
              )}
            </div>
          )}
          <div className="tabs" role="group" aria-label="Cleanup category">
            {(
              [
                ["duplicates", "Duplicate people"],
                ["failed", "Failed files"],
                ["missing", "Missing files"],
                ["deleted", "Deleted records"],
                ["originals", "Kept originals"],
                ["converted", "Conversion backups"],
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
          {tab === "duplicates" &&
            (data.possible_people.length ? (
              <>
                <p className="muted small-text">
                  Only stronger matches are shown. Use{" "}
                  <strong>Skip / not the same</strong> to hide a pair forever
                  and load fresher suggestions. Merge only when you are sure.
                  For photo/video file duplicates, open the{" "}
                  <Link to="/duplicates">Duplicates</Link> tab.
                </p>
                <div className="duplicate-grid">
                  {data.possible_people.map((pair) => (
                    <DuplicatePair
                      key={`${pair.a.id}:${pair.b.id}`}
                      {...pair}
                      onSkipped={() => resource.reload()}
                    />
                  ))}
                </div>
              </>
            ) : (
              <Empty
                icon="people"
                title="Every connection has its own place"
                description="No strong duplicate suggestions right now. After you skip noisy pairs or run a new scan, better matches can appear here. Photo and video duplicates are in the Duplicates tab."
              />
            ))}
          {tab === "failed" && <IssueMedia items={data.failed_media} />}
          {tab === "missing" && (
            <IssueMedia items={data.missing_media} missing />
          )}
          {tab === "originals" && (
            <SoftOriginalsPanel
              items={Array.isArray(data.soft_originals) ? data.soft_originals : []}
              onChanged={() => resource.reload()}
              onPurgeAll={() => setPurgeOriginalsOpen(true)}
            />
          )}
          {tab === "converted" && (
            <ConvertedBackupsPanel
              items={Array.isArray(data.converted_backups) ? data.converted_backups : []}
              onChanged={() => resource.reload()}
              onPurgeAll={() => setPurgeConvertedOpen(true)}
            />
          )}
          {tab === "deleted" && (
            <>
              <div className="collection-filters">
                <div
                  className="segmented"
                  role="group"
                  aria-label="Deleted record type"
                >
                  <button
                    aria-pressed={deletedTab === "faces"}
                    onClick={() => setDeletedTab("faces")}
                  >
                    Face records
                  </button>
                  <button
                    aria-pressed={deletedTab === "media"}
                    onClick={() => setDeletedTab("media")}
                  >
                    Media records
                  </button>
                </div>
                <span className="small-text muted">
                  Original files are never deleted.
                </span>
              </div>
              {deletedTab === "faces" ? (
                <DeletedFaces />
              ) : (
                <MediaCollection
                  tools={false}
                  filters={{ deleted: true, excluded: true }}
                  emptyTitle="No deleted media"
                  emptyDescription="Soft-deleted media records will appear here for restoration."
                />
              )}
            </>
          )}
        </>
      )}
    </>
  );
}
