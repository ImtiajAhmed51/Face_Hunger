import { useEffect, useState } from "react";
import { queryString, request } from "../api";
import { useAnimatedList, useResource, useSelection } from "../hooks";
import type { Face, Media, MediaDetail, Page } from "../types";
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
  const mediaResource = useResource<Page<Media>>(
    deleted
      ? null
      : `/media?${queryString({ people: [personId], excluded: true, page, limit: 24 })}`,
  );
  const [state, setState] = useState<{
    faces: Face[];
    loading: boolean;
    error: string;
  }>({ faces: [], loading: false, error: "" });
  const selection = useSelection(`${personId}:${page}:${deleted}`);
  const [viewer, setViewer] = useState<Face | null>(null);
  useEffect(() => {
    if (!mediaResource.data) return;
    const controller = new AbortController();
    setState((prev) => ({ ...prev, loading: !prev.faces.length, error: "" }));
    void Promise.all(
      mediaResource.data.items.map((item) =>
        request<MediaDetail>(`/media/${item.id}`, {
          signal: controller.signal,
        }),
      ),
    )
      .then((details) => {
        if (!controller.signal.aborted)
          setState({
            faces: details
              .flatMap((item) => item.faces)
              .filter(
                (face) => face.person_id === personId && !face.deleted_at,
              ),
            loading: false,
            error: "",
          });
      })
      .catch((error: Error) => {
        if (!controller.signal.aborted)
          setState({ faces: [], loading: false, error: error.message });
      });
    return () => controller.abort();
  }, [mediaResource.data, personId]);
  const loading = mediaResource.loading || state.loading;
  const animatedActive = useAnimatedList(state.faces, (face) => face.id);
  return (
    <section>
      <div className="collection-filters">
        <label className="check-label">
          <input
            type="checkbox"
            checked={deleted}
            onChange={(event) => setDeleted(event.target.checked)}
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
                disabled={!state.faces.length || loading}
                checked={
                  !!state.faces.length &&
                  state.faces.every((face) => selection.selected.has(face.id))
                }
                onChange={() =>
                  selection.all(state.faces.map((face) => face.id))
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
              faces={state.faces.filter((face) =>
                selection.selected.has(face.id),
              )}
              onDone={selection.clear}
            />
          )}
          <ErrorNotice
            error={mediaResource.error || state.error}
            retry={mediaResource.reload}
          />
          {loading ? (
            <Loading label="Loading this person's faces" />
          ) : !state.faces.length && !mediaResource.error && !state.error ? (
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
          {mediaResource.data && (
            <Pagination
              page={page}
              total={mediaResource.data.total}
              limit={24}
              onPage={setPage}
            />
          )}
        </>
      )}
      {viewer && (
        <MediaViewer
          id={viewer.media_id}
          timestamp={viewer.timestamp}
          ids={mediaResource.data?.items.map((item) => item.id)}
          onClose={() => setViewer(null)}
        />
      )}
    </section>
  );
}
