import { useState } from "react";
import { Link } from "react-router-dom";
import { number, queryString } from "../api";
import { useDebounced, useResource } from "../hooks";
import type { Page, Person } from "../types";
import { Icon } from "../components/Icon";
import {
  Badge,
  Empty,
  ErrorNotice,
  Loading,
  PageHeader,
  Pagination,
  Thumbnail,
} from "../components/ui";

export function Clusters() {
  const [q, setQ] = useState("");
  const query = useDebounced(q);
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState(1);
  const resource = useResource<Page<Person>>(
    `/clusters?${queryString({ q: query, page, limit: 36, samples: 12 })}`,
  );
  const items = resource.data?.items ?? [];

  const zoomOut = () =>
    setZoom((z) => Math.max(0.55, Math.round((z - 0.15) * 100) / 100));
  const zoomIn = () =>
    setZoom((z) => Math.min(1.85, Math.round((z + 0.15) * 100) / 100));
  const zoomReset = () => setZoom(1);

  return (
    <>
      <PageHeader
        eyebrow="SEE THE GROUPS"
        title="Face clusters"
        description="Each card is one person cluster — small faces that the engine grouped together. Zoom to inspect tiles."
        actions={
          <div className="cluster-zoom-controls" role="group" aria-label="Zoom">
            <button
              className="button small"
              type="button"
              onClick={zoomOut}
              aria-label="Zoom out"
              disabled={zoom <= 0.55}
            >
              <Icon name="close" size={14} />
              <span>−</span>
            </button>
            <button
              className="button small"
              type="button"
              onClick={zoomReset}
              aria-label="Reset zoom"
            >
              {Math.round(zoom * 100)}%
            </button>
            <button
              className="button small"
              type="button"
              onClick={zoomIn}
              aria-label="Zoom in"
              disabled={zoom >= 1.85}
            >
              <Icon name="plus" size={14} />
            </button>
          </div>
        }
      />
      <div className="collection-filters">
        <label className="input-icon">
          <Icon name="search" size={18} />
          <input
            type="search"
            aria-label="Search clusters by name"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(1);
            }}
            placeholder="Find a cluster..."
          />
        </label>
        <span className="muted">
          {resource.data
            ? `${number(resource.data.total)} clusters`
            : "Local face groups"}
        </span>
      </div>
      <ErrorNotice error={resource.error} retry={resource.reload} />
      {resource.loading && !resource.data ? (
        <Loading label="Loading clusters" />
      ) : resource.data && !items.length ? (
        <Empty
          icon="people"
          title={q ? "No cluster by that name" : "No clusters yet"}
          description={
            q
              ? "Try another name, or clear the search."
              : "Connect a library and run a scan. Faces will group into clusters here."
          }
        >
          {q ? (
            <button
              className="button"
              type="button"
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
        <div className="cluster-viewport">
          <div
            className="cluster-scale"
            style={{
              transform: `scale(${zoom})`,
              transformOrigin: "top left",
              width: `${100 / zoom}%`,
            }}
          >
            <div className="cluster-board">
              {items.map((person) => {
                const samples =
                  person.sample_face_ids && person.sample_face_ids.length
                    ? person.sample_face_ids
                    : person.representative_face_id
                      ? [person.representative_face_id]
                      : [];
                return (
                  <Link
                    key={person.id}
                    className="cluster-card"
                    to={`/people/${person.id}`}
                    title={person.display_name}
                  >
                    <div className="cluster-card-head">
                      <strong>{person.display_name}</strong>
                      <Badge>{number(person.face_count)} faces</Badge>
                    </div>
                    <div className="cluster-face-mosaic" aria-hidden="true">
                      {samples.length ? (
                        samples.map((fid) => (
                          <span className="cluster-face-tile" key={fid}>
                            <Thumbnail
                              src={`/api/faces/${fid}/thumbnail`}
                              alt=""
                              icon="people"
                            />
                          </span>
                        ))
                      ) : (
                        <span className="cluster-face-tile empty">
                          <Thumbnail src={null} alt="" icon="people" />
                        </span>
                      )}
                    </div>
                    <div className="cluster-card-meta">
                      <span>
                        <Icon name="photo" size={12} />
                        {number(person.photo_count)}
                      </span>
                      <span>
                        <Icon name="video" size={12} />
                        {number(person.video_count)}
                      </span>
                      {!!person.unreviewed_count && (
                        <Badge tone="amber">
                          {number(person.unreviewed_count)} review
                        </Badge>
                      )}
                    </div>
                  </Link>
                );
              })}
            </div>
          </div>
        </div>
      )}
      {resource.data && (
        <Pagination
          page={page}
          limit={36}
          total={resource.data.total}
          onPage={setPage}
        />
      )}
    </>
  );
}
