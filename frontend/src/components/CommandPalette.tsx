import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { mutate, queryString } from "../api";
import { useApp } from "../context";
import { useDebounced, useResource } from "../hooks";
import { rankCommands, type Command } from "../commands";
import type { Page, Person, SavedSearch } from "../types";
import { Icon } from "./Icon";
import { Dialog } from "./ui";

export interface NavTarget { to: string; label: string }

/** Ctrl/Cmd+K: one place to jump to pages, people, saved searches and actions. */
export function CommandPalette({ open, onClose, pages }: { open: boolean; onClose: () => void; pages: NavTarget[] }) {
  const navigate = useNavigate();
  const { setTheme, theme, undoLast, canUndo, notify } = useApp();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const term = useDebounced(query.trim(), 120);
  const people = useResource<Page<Person>>(open && term ? `/people?${queryString({ q: term, limit: 6 })}` : null);
  const saved = useResource<{ items: SavedSearch[] }>(open ? "/search/saved" : null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    // showModal() runs in the Dialog's effect and focuses the first button; take focus back.
    const timer = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    const go = (to: string) => () => { onClose(); navigate(to); };
    const list: Command[] = pages.map((page) => ({ id: `go:${page.to}`, group: "Go to", label: page.label, run: go(page.to) }));
    for (const person of people.data?.items ?? []) {
      list.push({ id: `person:${person.id}`, group: "People", label: person.display_name,
        hint: `${person.face_count} faces`, keywords: "person profile", run: go(`/people/${person.id}`) });
      list.push({ id: `person-search:${person.id}`, group: "People", label: `Photos with ${person.display_name}`,
        keywords: "search find media", run: go(`/search?${queryString({ q: person.display_name })}`) });
    }
    for (const item of saved.data?.items ?? []) {
      list.push({ id: `saved:${item.id}`, group: "Saved searches", label: item.name, keywords: "saved search",
        run: go(`/search?saved=${item.id}`) });
    }
    const themes: Record<string, string> = { light: "Light", dark: "Dark", system: "System" };
    for (const [value, label] of Object.entries(themes)) {
      if (value !== theme) list.push({ id: `theme:${value}`, group: "Actions", label: `Theme: ${label}`, keywords: "appearance colour mode",
        run: () => { setTheme(value as typeof theme); onClose(); } });
    }
    if (canUndo) list.push({ id: "undo", group: "Actions", label: "Undo last action", hint: "Ctrl/⌘ Z", run: () => { onClose(); void undoLast(); } });
    list.push({ id: "backfill", group: "Actions", label: "Compute missing embeddings", keywords: "siglip dino index background job",
      run: () => { onClose(); void mutate("/jobs", { kind: "embed_backfill", priority: 80 }).then(() => notify("Embedding job queued."), (e) => notify(String(e), true)); } });
    if (query.trim()) {
      list.push({ id: "search", group: "Search", label: `Search for “${query.trim()}”`, hint: "Enter",
        run: go(`/search?${queryString({ q: query.trim() })}`) });
    }
    return list;
  }, [pages, people.data, saved.data, theme, canUndo, query, navigate, onClose, setTheme, undoLast, notify]);

  const results = useMemo(() => rankCommands(commands, query), [commands, query]);
  const current = Math.min(active, Math.max(0, results.length - 1));
  useEffect(() => {
    listRef.current?.querySelector(`[data-i="${current}"]`)?.scrollIntoView({ block: "nearest" });
  }, [current]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") { event.preventDefault(); setActive((current + 1) % Math.max(1, results.length)); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setActive((current - 1 + results.length) % Math.max(1, results.length)); }
    else if (event.key === "Enter") { event.preventDefault(); results[current]?.run(); }
    else if (event.key === "Home") { event.preventDefault(); setActive(0); }
    else if (event.key === "End") { event.preventDefault(); setActive(results.length - 1); }
  };

  let lastGroup = "";
  return (
    <Dialog open={open} onClose={onClose} title="Command palette" className="command-palette">
      <div className="palette-input">
        <Icon name="search" size={18} />
        <input
          ref={inputRef}
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-list"
          aria-activedescendant={results[current] ? `palette-${results[current].id}` : undefined}
          aria-autocomplete="list"
          aria-label="Type a command, page, person or search"
          placeholder="Jump to a page, a person, or search your memories…"
          value={query}
          onChange={(event) => { setQuery(event.target.value); setActive(0); }}
          onKeyDown={onKeyDown}
        />
      </div>
      <ul className="palette-list" id="palette-list" role="listbox" aria-label="Results" ref={listRef}>
        {results.length === 0 && <li className="palette-empty" role="presentation">No matches</li>}
        {results.map((command, index) => {
          const header = command.group !== lastGroup ? command.group : null;
          lastGroup = command.group;
          return [
            header && <li key={`g:${header}`} className="palette-group" role="presentation">{header}</li>,
            <li key={command.id} id={`palette-${command.id}`} data-i={index} role="option" aria-selected={index === current}
              className="palette-option" onMouseMove={() => setActive(index)} onClick={() => command.run()}>
              <span>{command.label}</span>
              {command.hint && <span className="palette-hint">{command.hint}</span>}
            </li>,
          ];
        })}
      </ul>
      <div className="palette-footer" aria-hidden="true">
        <span>↑↓ navigate</span><span>Enter open</span><span>Esc close</span>
      </div>
    </Dialog>
  );
}
