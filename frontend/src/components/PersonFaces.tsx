import { useState } from "react";
import { queryString } from "../api";
import { useAnimatedList, useResource, useSelection } from "../hooks";
import type { Face, Page } from "../types";
import { FaceActions, FaceTile } from "./FaceActions";
import { MediaViewer } from "./MediaViewer";
import { Empty, ErrorNotice, Loading, Pagination } from "./ui";

export function DeletedFaces({ personId }: { personId?: number }) {
  const [page, setPage] = useState(1);
  const path = `/review?${queryString({ deleted: true, person_id: personId, page, limit: 30 })}`;
  const resource = useResource<Page<Face>>(path);
  const selection = useSelection(path);
  const [viewer, setViewer] = useState<Face | null>(null);
  const items = resource.data?.items ?? [];
  const animatedFaces = useAnimatedList(items, (face) => face.id);
  return (
    <section>
      <div className="collection-bar">
        <label className="check-label">
          <input
            type="checkbox"
            disabled={!items.length}
            checked={
              !!items.length &&
              items.every((face) => selection.selected.has(face.id))
            }
            onChange={() => selection.all(items.map((face) => face.id))}
          />
          Select page
        </label>
        <span className="muted">{resource.data?.total ?? 0} deleted faces</span>
      </div>
      <FaceActions
        faces={items.filter((face) => selection.selected.has(face.id))}
        allowPermanent
        onDone={selection.clear}
      />
      <ErrorNotice error={resource.error} retry={resource.reload} />
      {resource.loading && !resource.data ? (
        <Loading label="Loading deleted faces" />
      ) : resource.data && !items.length ? (
        <Empty
          icon="restore"
          title="No deleted faces"
          description="Soft-deleted faces appear here. Restore them or permanently remove their records without touching original files."
        />
      ) : (
        <div className="face-grid">
          {animatedFaces.map(({ item: face, key, phase }) => (
            <div key={key} className={`anim-item anim-${phase}`}>
              <FaceTile
                face={face}
                selected={selection.selected.has(face.id)}
                onSelect={() => selection.toggle(face.id)}
                onOpen={() => setViewer(face)}
              />
            </div>
          ))}
        </div>
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
    </section>
  );
}

export function PersonFaces({ personId }: { personId: number }) {
  const [page, setPage] = useState(1);
  const [deleted, setDeleted] = useState(false);
  const path = `/people/${personId}/faces?${queryString({ page, limit: 60, deleted })}`;
  const resource = useResource<Page<Face>>(deleted ? null : path);
  const selection = useSelection(path);
  const [viewer, setViewer] = useState<Face | null>(null);
  const faces = resource.data?.items ?? [];
  const loading = resource.loading;
  const animatedActive = useAnimatedList(faces, (face) => face.id);
  return (
    <section>
      <div className="collection-filters">
        <label className="check-label">
          <input
            type="checkbox"
            checked={deleted}
            onChange={(event) => { setDeleted(event.target.checked); setPage(1); }}
          />
          Deleted faces
        </label>
        <span className="muted small-text">
          Select individual faces to correct, split, move, or exclude them.
        </span>
      </div>
      {deleted ? (
        <DeletedFaces personId={personId} />
      ) : (
        <>
          <div className="collection-bar">
            <label className="check-label">
              <input
                type="checkbox"
                disabled={!faces.length || loading}
                checked={
                  !!faces.length &&
                  faces.every((face) => selection.selected.has(face.id))
                }
                onChange={() =>
                  selection.all(faces.map((face) => face.id))
                }
              />
              Select faces on page
            </label>
            <span className="muted small-text">
              Faces grouped by source file
            </span>
          </div>
          {!loading && (
            <FaceActions
              faces={faces.filter((face) =>
                selection.selected.has(face.id),
              )}
              onDone={selection.clear}
            />
          )}
          <ErrorNotice
            error={resource.error}
            retry={resource.reload}
          />
          {loading ? (
            <Loading label="Loading this person's faces" />
          ) : !faces.length && !resource.error ? (
            <Empty
              icon="people"
              title="No active faces on this page"
              description="Faces assigned to this person appear here. Switch to deleted faces to restore removed records."
            />
          ) : (
            <div className="face-grid">
              {animatedActive.map(({ item: face, key, phase }) => (
                <div key={key} className={`anim-item anim-${phase}`}>
                  <FaceTile
                    face={face}
                    selected={selection.selected.has(face.id)}
                    onSelect={() => selection.toggle(face.id)}
                    onOpen={() => setViewer(face)}
                  />
                </div>
              ))}
            </div>
          )}
          {resource.data && (
            <Pagination
              page={page}
              total={resource.data.total}
              limit={60}
              onPage={setPage}
            />
          )}
        </>
      )}
      {viewer && (
        <MediaViewer
          id={viewer.media_id}
          timestamp={viewer.timestamp}
          ids={[...new Set(faces.map((face) => face.media_id))]}
          onClose={() => setViewer(null)}
        />
      )}
    </section>
  );
}
