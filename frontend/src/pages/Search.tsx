import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { mutate } from "../api";
import { useAction } from "../context";
import { useResource, refreshData } from "../hooks";
import type { HybridQuery, MediaFilters, ParsedSearch, SavedSearch } from "../types";
import { HybridResults } from "../components/HybridResults";
import { Icon } from "../components/Icon";
import { MediaCollection } from "../components/MediaGrid";
import { PersonPicker, PersonToken } from "../components/PersonPicker";
import {
  Badge,
  Dialog,
  Empty,
  ErrorNotice,
  PageHeader,
} from "../components/ui";

const initial: MediaFilters = {
  people: [],
  mode: "ANY",
  kind: "",
  date_from: "",
  date_to: "",
  confidence: "",
  reviewed: "",
  excluded: false,
  deleted: false,
  q: "",
};

/** Filters only the legacy /media listing understands (fallback path). */
const usesLegacyFilters = (f: MediaFilters) => f.confidence !== "" || !!f.reviewed || !!f.excluded;

function toHybrid(f: MediaFilters, content: string, similar: number | null): HybridQuery {
  const q: HybridQuery = { people: f.people ?? [], people_mode: f.mode ?? "ANY" };
  if (content.trim()) q.text = content.trim();
  if (f.kind) q.kind = f.kind;
  if (f.date_from) q.date_from = f.date_from;
  if (f.date_to) q.date_to = f.date_to;
  if (similar !== null) q.similar_media_id = similar;
  if (f.q) q.name = f.q;
  if (f.deleted) q.deleted = true;
  return q;
}

function fromHybrid(q: HybridQuery): MediaFilters {
  return { ...initial, people: q.people ?? [], mode: q.people_mode ?? "ANY", kind: q.kind ?? "",
    date_from: q.date_from ?? "", date_to: q.date_to ?? "" };
}

export function Search() {
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get("q") ?? "");
  const [content, setContent] = useState("");
  const [appliedContent, setAppliedContent] = useState("");
  const similar = Number(params.get("similar")) || null;
  const saved = useResource<{ items: SavedSearch[] }>("/search/saved");
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [draft, setDraft] = useState<MediaFilters>(initial);
  const [applied, setApplied] = useState<MediaFilters | null>(null);
  const [unmatched, setUnmatched] = useState<string[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [picker, setPicker] = useState(false);
  const [validation, setValidation] = useState("");
  const action = useAction();
  const update = <K extends keyof MediaFilters>(
    key: K,
    value: MediaFilters[K],
  ) => setDraft((current) => ({ ...current, [key]: value }));
  const apply = () => {
    if (draft.date_from && draft.date_to && draft.date_from > draft.date_to) {
      setValidation("The end date must be on or after the start date.");
      return;
    }
    setValidation("");
    setApplied({ ...draft });
    setAppliedContent(content);
  };
  const runParse = (text: string) =>
    action.run(
      async () => {
        const parsed = await mutate<ParsedSearch>("/search/parse", { query: text });
        const next: MediaFilters = {
          ...initial,
          people: parsed.people,
          mode: parsed.mode,
          kind: parsed.kind ?? "",
          date_from: parsed.date_from ?? "",
          date_to: parsed.date_to ?? "",
        };
        setDraft(next);
        setApplied(next);
        setContent(parsed.text ?? "");
        setAppliedContent(parsed.text ?? "");
        // Leftover words become a content search when SigLIP 2 is installed.
        setUnmatched(parsed.embedding_query?.available ? [] : parsed.unmatched);
        setExpanded(true);
        setValidation("");
      },
      undefined,
      false,
    );
  // Deep links: /search?q=... (command palette) and /search?similar=<media id> (viewer).
  const initialQ = params.get("q");
  useEffect(() => {
    if (initialQ) void runParse(initialQ);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQ]);
  // Arriving via "Find similar" starts a fresh query around that item.
  useEffect(() => {
    if (similar === null || params.get("saved") || params.get("q")) return;
    setDraft(initial);
    setApplied({ ...initial });
    setContent("");
    setAppliedContent("");
    setUnmatched([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [similar]);
  const savedId = Number(params.get("saved")) || null;
  useEffect(() => {
    const item = saved.data?.items.find((entry) => entry.id === savedId);
    if (!item) return;
    const next = fromHybrid(item.query);
    setDraft(next);
    setApplied(next);
    setContent(item.query.text ?? "");
    setAppliedContent(item.query.text ?? "");
    setExpanded(true);
  }, [savedId, saved.data]);
  const hybridQuery = useMemo(
    () => (applied && !usesLegacyFilters(applied) ? toHybrid(applied, appliedContent, similar) : null),
    [applied, appliedContent, similar],
  );
  return (
    <>
      <PageHeader
        eyebrow="LOOK FOR A PERSON. FIND A MOMENT."
        title="Search your memories"
        description="Ordinary words. Precise filters. Parsed locally, without sending anything away."
      />
      <form
        className="natural-search"
        onSubmit={(event) => {
          event.preventDefault();
          if (!query.trim()) return;
          setParams(similar ? { similar: String(similar) } : {});
          void runParse(query.trim());
        }}
      >
        <Icon name="search" size={24} />
        <label className="sr-only" htmlFor="natural-query">
          Search by people, dates, and media type
        </label>
        <input
          id="natural-query"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="A name, a year, or what's in the picture: “Maya at the beach 2021”"
          autoComplete="off"
        />
        <button
          className="button primary"
          disabled={action.busy || !query.trim()}
        >
          {action.busy ? "Reading your search..." : "Find moments"}
          <Icon name="arrow" size={16} />
        </button>
      </form>
      <div className="search-hint">
        <span>
          <Icon name="shield" size={14} />
          Parsed and searched on this device, not by a cloud chatbot
        </span>
        <button
          className="text-link"
          aria-expanded={expanded}
          aria-controls="structured-filters"
          onClick={() => setExpanded((value) => !value)}
        >
          <Icon name="filter" size={16} />
          {expanded ? "Hide filters" : "Build a precise search"}
        </button>
      </div>
      <ErrorNotice error={action.error} />
      {unmatched.length > 0 && (
        <div className="search-unmatched" role="status">
          <Icon name="alert" size={18} />
          <div>
            <strong>Some words weren't understood</strong>
            <p>{unmatched.join(", ")}</p>
            <p>
              These words are not applied as filters. Add a person, date, or
              filename below to refine the results.
            </p>
          </div>
        </div>
      )}
      {expanded && (
        <form
          className="filter-panel"
          id="structured-filters"
          onSubmit={(event) => {
            event.preventDefault();
            apply();
          }}
        >
          <div className="filter-panel-heading">
            <h2>Make it specific</h2>
            <button
              type="button"
              className="text-link"
              onClick={() => {
                setDraft(initial);
                setApplied(null);
                setContent("");
                setAppliedContent("");
                setParams({});
                setQuery("");
                setUnmatched([]);
                setValidation("");
              }}
            >
              Reset search
            </button>
          </div>
          <label className="field wide-field">
            What's in the picture
            <input
              type="search"
              value={content}
              onChange={(event) => setContent(event.target.value)}
              placeholder="e.g. dog on the beach, birthday cake, snow"
            />
          </label>
          <div className="people-filter">
            <label className="field-label">People</label>
            <div className="people-filter-tokens">
              {draft.people?.map((id) => (
                <PersonToken
                  key={id}
                  id={id}
                  onRemove={() =>
                    update(
                      "people",
                      draft.people?.filter((value) => value !== id),
                    )
                  }
                />
              ))}
              <button
                type="button"
                className="button small"
                onClick={() => setPicker(true)}
              >
                <Icon name="plus" size={16} />
                Add person
              </button>
              {!draft.people?.length && (
                <span className="muted small-text">
                  Everyone in your library
                </span>
              )}
            </div>
          </div>
          <div className="filter-grid">
            <label className="field">
              People matching
              <select
                value={draft.mode}
                onChange={(event) =>
                  update("mode", event.target.value as "ANY" | "ALL")
                }
              >
                <option value="ANY">ANY selected person</option>
                <option value="ALL">ALL selected people together</option>
              </select>
            </label>
            <label className="field">
              Media type
              <select
                value={draft.kind}
                onChange={(event) =>
                  update("kind", event.target.value as MediaFilters["kind"])
                }
              >
                <option value="">Photos & videos</option>
                <option value="photo">Photos</option>
                <option value="video">Videos</option>
              </select>
            </label>
            <label className="field">
              From date
              <input
                type="date"
                value={draft.date_from}
                onChange={(event) => update("date_from", event.target.value)}
              />
            </label>
            <label className="field">
              Through date
              <input
                type="date"
                min={draft.date_from || undefined}
                value={draft.date_to}
                onChange={(event) => update("date_to", event.target.value)}
              />
            </label>
            <label className="field">
              Minimum match confidence
              <input
                type="number"
                min="0"
                max="1"
                step="0.01"
                placeholder="Any confidence (0-1)"
                value={draft.confidence}
                onChange={(event) =>
                  update(
                    "confidence",
                    event.target.value === "" ? "" : Number(event.target.value),
                  )
                }
              />
            </label>
            <label className="field">
              Review state
              <select
                value={draft.reviewed}
                onChange={(event) =>
                  update(
                    "reviewed",
                    event.target.value as MediaFilters["reviewed"],
                  )
                }
              >
                <option value="">Any review state</option>
                <option value="confirmed">Confirmed</option>
                <option value="unreviewed">Unreviewed</option>
              </select>
            </label>
            <label className="field wide-field">
              Filename contains
              <input
                type="search"
                value={draft.q}
                onChange={(event) => update("q", event.target.value)}
                placeholder="Filter the original filename"
              />
            </label>
          </div>
          <div className="filter-footer">
            <div className="inline-actions">
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={!!draft.excluded}
                  onChange={(event) => update("excluded", event.target.checked)}
                />
                Include excluded matches
              </label>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={!!draft.deleted}
                  onChange={(event) => update("deleted", event.target.checked)}
                />
                Deleted media only
              </label>
            </div>
            <button className="button primary">
              Apply filters
              <Icon name="arrow" size={16} />
            </button>
          </div>
          <ErrorNotice error={validation} />
        </form>
      )}
      {applied ? (
        <>
          <div className="results-heading">
            <h2>Your results</h2>
            <Badge tone="teal">
              {applied.mode === "ALL"
                ? "All selected people"
                : "Any selected person"}
            </Badge>
            {hybridQuery && (
              <button className="button small" onClick={() => { setSaveName(query || appliedContent || "My search"); setSaveOpen(true); }}>
                <Icon name="plus" size={16} />
                Save search
              </button>
            )}
          </div>
          {similar !== null && (
            <div className="inline-actions">
              <Badge tone="teal">Looks like media #{similar}</Badge>
              <button className="text-link" onClick={() => setParams({})}>Clear</button>
            </div>
          )}
          {hybridQuery ? (
            <HybridResults query={hybridQuery} />
          ) : (
            <MediaCollection
              filters={applied}
              tools={false}
              emptyTitle="No moments match just yet"
              emptyDescription="Try a wider date range, ANY instead of ALL, fewer people, or include excluded matches. Only indexed files can appear in search."
            />
          )}
        </>
      ) : (
        <Empty
          icon="search"
          title="A little less searching. A little more finding."
          description="Use the names you've added to your library. Combine people with dates or a media type, then fine-tune the filters to find just the right moment."
        />
      )}
      {!!saved.data?.items.length && (
        <section className="saved-searches" aria-label="Saved searches">
          <h2 className="field-label">Saved searches</h2>
          <div className="inline-actions" style={{ flexWrap: "wrap" }}>
            {saved.data.items.map((item) => (
              <span key={item.id} className="inline-actions">
                <button className="button small" onClick={() => {
                  const next = fromHybrid(item.query);
                  setDraft(next);
                  setApplied(next);
                  setContent(item.query.text ?? "");
                  setAppliedContent(item.query.text ?? "");
                  setParams(item.query.similar_media_id ? { similar: String(item.query.similar_media_id) } : {});
                  setExpanded(true);
                  void mutate(`/search/saved/${item.id}/run`, { limit: 1 });
                }}>
                  <Icon name="search" size={14} />
                  {item.name}
                </button>
                <button className="icon-button" aria-label={`Delete saved search ${item.name}`}
                  onClick={() => void action.run(() => mutate(`/search/saved/${item.id}`, undefined, "DELETE"), "Saved search removed.")}>
                  <Icon name="close" size={14} />
                </button>
              </span>
            ))}
          </div>
        </section>
      )}
      <Dialog open={saveOpen} onClose={() => setSaveOpen(false)} title="Save this search">
        <form onSubmit={async (event) => {
          event.preventDefault();
          if (!hybridQuery || !saveName.trim()) return;
          const { page: _page, limit: _limit, ...rest } = hybridQuery;
          void _page; void _limit;
          if (await action.run(() => mutate("/search/saved", { name: saveName.trim(), query: rest }), "Search saved.")) {
            setSaveOpen(false);
            refreshData();
          }
        }}>
          <div className="dialog-body">
            <label className="field">Name<input autoFocus value={saveName} onChange={(event) => setSaveName(event.target.value)} required /></label>
          </div>
          <div className="dialog-footer">
            <button type="button" className="button" onClick={() => setSaveOpen(false)}>Cancel</button>
            <button className="button primary" disabled={!saveName.trim() || action.busy}>Save</button>
          </div>
        </form>
      </Dialog>
      <Dialog
        open={picker}
        onClose={() => setPicker(false)}
        title="Who are you looking for?"
      >
        <div className="dialog-body">
          <PersonPicker
            excluded={draft.people}
            onSelect={(person) => {
              update("people", [...(draft.people ?? []), person.id]);
              setPicker(false);
            }}
          />
        </div>
      </Dialog>
    </>
  );
}
