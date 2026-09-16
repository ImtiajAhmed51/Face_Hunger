import { useState } from "react";
import { Link } from "react-router-dom";
import { mutate, number } from "../api";
import { useAction, useApp } from "../context";
import { Badge, ConfirmDialog, ErrorNotice } from "./ui";
import { Icon } from "./Icon";

export function JobCard({ compact = false }: { compact?: boolean }) {
  const { job, jobError } = useApp();
  const action = useAction();
  const [cancel, setCancel] = useState(false);
  if (!job)
    return compact ? (
      <div className="local-status">
        <Icon name="shield" size={16} />
        <span>
          {jobError ? "Local server unavailable" : "Private. On this device."}
        </span>
      </div>
    ) : (
      <ErrorNotice error={jobError} />
    );
  const active = ["queued", "running", "paused"].includes(job.status);
  const progress = job.total
    ? Math.min(100, Math.round((job.processed / job.total) * 100))
    : 0;
  if (compact)
    return (
      <Link className="sidebar-job" to="/settings">
        <span className={`status-dot ${active ? "active" : ""}`} />
        <span>
          {active
            ? `${job.status === "paused" ? "Paused" : "Indexing"} · ${progress}%`
            : `Scan ${job.status}`}
        </span>
        <Icon name="arrow" size={14} />
      </Link>
    );
  return (
    <section className="job-card" aria-label="Indexing job">
      <div className="job-top">
        <div className="job-icon">
          <Icon name="spark" />
        </div>
        <div>
          <p className="eyebrow">LOCAL INDEX · JOB {job.id}</p>
          <h3>
            {active
              ? job.status === "paused"
                ? "Taking a pause"
                : "Finding the familiar"
              : `Indexing ${job.status}`}
          </h3>
        </div>
        <Badge tone={job.status === "failed" ? "red" : active ? "teal" : ""}>
          {job.status}
        </Badge>
      </div>
      <div className="job-progress">
        <progress
          max={Math.max(1, job.total)}
          value={Math.min(job.processed, Math.max(1, job.total))}
          aria-label="Indexing progress"
        />
        <span>
          {number(job.processed)} / {number(job.total)}
        </span>
      </div>
      <div className="job-stats">
        <span>{job.phase}</span>
        <span>{number(job.faces)} faces</span>
        <span>{number(job.people)} people</span>
        <span>{number(job.skipped)} skipped</span>
        <span>{number(job.failed)} failed</span>
      </div>
      {job.current_file && (
        <p className="current-file" title={job.current_file}>
          {job.current_file}
        </p>
      )}
      <ErrorNotice error={job.error || jobError} />
      <div className="job-actions">
        {active ? (
          <>
            <button
              className="button small"
              disabled={action.busy}
              onClick={() =>
                void action.run(
                  () =>
                    mutate(
                      `/index/${job.id}/${job.status === "paused" ? "resume" : "pause"}`,
                    ),
                  job.status === "paused"
                    ? "Indexing resumed."
                    : "Pause requested.",
                )
              }
            >
              <Icon
                name={job.status === "paused" ? "play" : "pause"}
                size={14}
              />
              {job.status === "paused" ? "Resume" : "Pause"}
            </button>
            <button
              className="button ghost small"
              onClick={() => setCancel(true)}
              disabled={action.busy}
            >
              Cancel scan
            </button>
          </>
        ) : (
          ["failed", "interrupted", "cancelled"].includes(job.status) && (
            <Link className="text-link" to="/settings">
              Start a new scan in Libraries
              <Icon name="arrow" size={14} />
            </Link>
          )
        )}
      </div>
      <ConfirmDialog
        open={cancel}
        onClose={() => setCancel(false)}
        title="Cancel this scan?"
        description="Work already indexed is kept. You can start another scan from Libraries at any time."
        label="Cancel scan"
        onConfirm={() => mutate(`/index/${job.id}/cancel`)}
        success="Cancellation requested."
      />
    </section>
  );
}
