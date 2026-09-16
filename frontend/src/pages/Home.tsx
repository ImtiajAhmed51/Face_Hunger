import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { mutate, number } from "../api";
import { useAction, useApp } from "../context";
import { useResource } from "../hooks";
import type { Dashboard, Engine } from "../types";
import { Icon } from "../components/Icon";
import type { IconName } from "../components/Icon";
import { JobCard } from "../components/JobCard";
import { MediaGrid } from "../components/MediaGrid";
import { MediaViewer } from "../components/MediaViewer";
import {
  Badge,
  ErrorNotice,
  Loading,
  PageHeader,
  SectionHeading,
  Thumbnail,
} from "../components/ui";

export function EngineStatus({ engine }: { engine: Engine }) {
  const action = useAction();
  return (
    <div className="engine-status">
      <div className="inline-actions">
        <span
          className={`status-dot ${engine.state === "ready" ? "active" : ""}`}
        />
        <strong>Local face engine</strong>
        <Badge
          tone={
            engine.state === "ready"
              ? "teal"
              : engine.state === "error"
                ? "red"
                : ""
          }
        >
          {engine.state}
        </Badge>
      </div>
      <p>
        {engine.provider ?? "No provider loaded"} · {engine.model}
      </p>
      {engine.error && <ErrorNotice error={engine.error} />}
      {engine.state !== "ready" && (
        <button
          className="button small"
          disabled={action.busy || engine.state === "loading"}
          onClick={() =>
            void action.run(() => mutate("/engine/load"), "Local model loaded.")
          }
        >
          <Icon name="spark" size={16} />
          {action.busy || engine.state === "loading"
            ? "Loading local model..."
            : "Load local model"}
        </button>
      )}
    </div>
  );
}

export function Home() {
  const resource = useResource<Dashboard>("/dashboard");
  const { job } = useApp();
  const [viewer, setViewer] = useState<number | null>(null);
  useEffect(() => {
    if (job?.status === "running") resource.reload();
  }, [job?.processed]);
  const data = resource.data;
  return (
    <>
      <PageHeader
        eyebrow="A LITTLE CLOSER TO YOUR MEMORIES"
        title="Your collection, connected."
        description="Find the people. Keep the moments. Everything stays here."
        actions={
          <Link className="button" to="/settings">
            <Icon name="plus" size={18} />
            Add a library
          </Link>
        }
      />
      <ErrorNotice error={resource.error} retry={resource.reload} />
      {resource.loading && !data && <Loading />}
      {data && (
        <>
          <div className="stats-grid">
            {(
              [
                ["Photos", data.photos, "photo", "/photos"],
                ["Videos", data.videos, "video", "/videos"],
                ["People", data.people, "people", "/people"],
                ["Faces found", data.faces, "spark", "/review"],
              ] as [string, number, IconName, string][]
            ).map(([label, value, icon, to]) => (
              <Link className="stat-card" to={to} key={label}>
                <div>
                  <span>{label}</span>
                  <Icon name={icon} size={20} />
                </div>
                <strong>{number(value)}</strong>
                <span className="stat-link">
                  Explore
                  <Icon name="arrow" size={14} />
                </span>
              </Link>
            ))}
          </div>
          {!data.photos && !data.videos ? (
            <section className="welcome-hero">
              <div className="welcome-copy">
                <span className="eyebrow">
                  START WITH A FOLDER. FIND A STORY.
                </span>
                <h2>
                  A place for every
                  <br />
                  <em>familiar face.</em>
                </h2>
                <p>
                  Bring your photos and videos together without moving a single
                  file. Stillroom finds the faces, you make the connections.
                </p>
                <Link className="button primary" to="/settings">
                  <Icon name="folder" size={18} />
                  Connect your first library
                  <Icon name="arrow" size={16} />
                </Link>
                <span className="privacy-caption">
                  <Icon name="shield" size={14} />
                  No uploads. No cloud. Just your memories.
                </span>
              </div>
              <div className="paper-art" aria-hidden="true">
                <div className="art-orbit" />
                <div className="art-card art-back">
                  <Icon name="video" size={44} />
                </div>
                <div className="art-card art-front">
                  <div className="art-frame">
                    <div className="art-head" />
                    <div className="art-shoulders" />
                    <i />
                    <i />
                    <i />
                    <i />
                  </div>
                  <span>A familiar face.</span>
                </div>
                <div className="art-note">made of moments</div>
                <div className="art-star">+</div>
              </div>
            </section>
          ) : (
            <>
              <section className="home-search">
                <div>
                  <span className="eyebrow">THE PEOPLE MAKE THE PICTURE</span>
                  <h2>A face. A name. A moment.</h2>
                </div>
                <Link className="home-search-link" to="/search">
                  <Icon name="search" />
                  <span>Search your memories, naturally...</span>
                  <kbd>/</kbd>
                </Link>
              </section>
              {data.recent_people.length > 0 && (
                <section className="home-section">
                  <SectionHeading
                    title="Familiar faces"
                    meta="The people in your collection"
                    to="/people"
                  />
                  <div className="people-strip">
                    {data.recent_people.map((person) => (
                      <Link
                        className="person-mini"
                        key={person.id}
                        to={`/people/${person.id}`}
                      >
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
                  </div>
                </section>
              )}
              {data.recent_media.length > 0 && (
                <section className="home-section">
                  <SectionHeading
                    title="From your collection"
                    meta="Recently indexed photos and videos"
                    to="/photos"
                  />
                  <MediaGrid items={data.recent_media} onOpen={setViewer} />
                </section>
              )}
            </>
          )}
          <div className="home-bottom">
            <section className="review-callout">
              <div className="callout-icon">
                <Icon name="review" size={26} />
              </div>
              <span className="eyebrow">A HUMAN TOUCH</span>
              <h2>
                {data.review_count
                  ? `${number(data.review_count)} faces could use your eye.`
                  : "You know them best."}
              </h2>
              <p>
                {data.review_count
                  ? "A quick yes or no makes your local collection more accurate. Your corrections always come first."
                  : "When the engine is unsure, a quick review helps it put a name to the right face."}
              </p>
              <Link className="text-link" to="/review">
                {data.review_count ? "Start a quick review" : "Open review"}
                <Icon name="arrow" size={16} />
              </Link>
            </section>
            <section className="local-panel">
              <span className="eyebrow">BUILT TO STAY PRIVATE</span>
              <h3>
                Your memories.
                <br />
                Your machine.
              </h3>
              <p>
                Originals stay in their folders. Face recognition runs locally.
                You stay in control.
              </p>
              <EngineStatus engine={data.engine} />
            </section>
          </div>
          {job && <JobCard />}
          {!!(
            data.cleanup.failed +
            data.cleanup.missing +
            data.cleanup.duplicates
          ) && (
            <Link className="cleanup-nudge" to="/cleanup">
              <Icon name="cleanup" />
              <span>
                {number(data.cleanup.duplicates)} duplicate candidates ·{" "}
                {number(data.cleanup.failed)} failed files ·{" "}
                {number(data.cleanup.missing)} missing files
              </span>
              <strong>Keep things tidy</strong>
              <Icon name="arrow" size={16} />
            </Link>
          )}
          {viewer !== null && (
            <MediaViewer
              id={viewer}
              ids={data.recent_media.map((item) => item.id)}
              onClose={() => setViewer(null)}
            />
          )}
        </>
      )}
    </>
  );
}
