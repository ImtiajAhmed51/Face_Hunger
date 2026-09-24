import { useCallback, useEffect, useRef, type RefObject } from "react";
import { flushSync } from "react-dom";

/**
 * Jump-free list mutations (delete / ignore / move / restore) for a scrolling grid.
 *
 * Why this exists
 * ---------------
 * The old approach kept a removed node in the DOM for ~340 ms (`anim-exit`) and
 * re-appended it to the END of the list, so a deleted group teleported thousands
 * of pixels down while it faded and everything (scroll anchor included) followed it.
 * FLIP rects were also stored in viewport coordinates, so any scroll between two
 * mutations made every card "animate" from a wrong position.
 *
 * How it works (everything below happens in ONE synchronous task, so the browser
 * never paints an intermediate state):
 *
 *   1. SNAPSHOT   read the boxes of every motion node within ±1 screen of the viewport.
 *   2. GHOST      clone the nodes being removed into a fixed-position layer (they leave
 *                 layout immediately and just fade out — layout is final from frame 0).
 *   3. COMMIT     flushSync(setState…) — React updates the DOM synchronously.
 *   4. ANCHOR     the first visible surviving leaf (DOM order) is pinned: if layout moved
 *                 it, window.scrollBy compensates in the same task. Nothing above the
 *                 change shifts; content below slides up.
 *   5. FLIP       every survivor animates from where it visually was to where it is now.
 *                 Deltas are PARENT-RELATIVE, so a card inside a moving group is not
 *                 animated twice.
 *   6. ENTER      nodes that did not exist before (e.g. a rollback) fade in.
 *
 * Markup contract: mark the direct children of the scroll root (the "slots") and the
 * leaves inside them with `data-motion-id`. Slots may contain leaves; leaves are the
 * scroll-anchor candidates.
 */

const EXIT_MS = 180;
/** Longer slides get more time so a 500 px glide never reads as a snap. */
const moveMs = (distance: number) => Math.round(Math.min(520, 260 + distance * 0.3));
const ENTER_MS = 220;
const EASE = "cubic-bezier(0.22, 1, 0.36, 1)";

interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
}

interface Snap {
  id: string;
  el: HTMLElement;
  box: Box;
  parent: string | null;
  leaf: boolean;
}

const boxOf = (el: Element): Box => {
  const r = el.getBoundingClientRect();
  return { top: r.top, left: r.left, width: r.width, height: r.height };
};
const bottomOf = (b: Box) => b.top + b.height;
/** Within one screen above/below the viewport: cheap to measure, worth animating. */
const inBand = (b: Box, vh: number) => bottomOf(b) > -vh && b.top < vh * 2;
const inView = (b: Box, vh: number) => bottomOf(b) > 0 && b.top < vh;

const parentIdOf = (el: HTMLElement) =>
  el.parentElement?.closest<HTMLElement>("[data-motion-id]")?.dataset.motionId ??
  null;

/** Boxes of slots (root children) and their leaves near the viewport, in DOM order. */
function snapshot(root: HTMLElement, vh: number): Map<string, Snap> {
  const out = new Map<string, Snap>();
  for (const child of Array.from(root.children)) {
    const slot = child as HTMLElement;
    const id = slot.dataset.motionId;
    if (!id) continue;
    const slotBox = boxOf(slot);
    if (!inBand(slotBox, vh)) continue;
    out.set(id, { id, el: slot, box: slotBox, parent: null, leaf: false });
    slot.querySelectorAll<HTMLElement>("[data-motion-id]").forEach((el) => {
      const leafId = el.dataset.motionId!;
      out.set(leafId, {
        id: leafId,
        el,
        box: boxOf(el),
        parent: parentIdOf(el),
        leaf: true,
      });
    });
  }
  return out;
}

function makeGhost(source: HTMLElement, b: Box): HTMLElement {
  const ghost = source.cloneNode(true) as HTMLElement;
  ghost.removeAttribute("data-motion-id");
  ghost.querySelectorAll("[data-motion-id]").forEach((n) => n.removeAttribute("data-motion-id"));
  // Already-decoded thumbnails: avoid a blank frame when the clone is painted.
  ghost.querySelectorAll("img").forEach((img) => {
    img.loading = "eager";
    img.decoding = "sync";
  });
  ghost.setAttribute("aria-hidden", "true");
  ghost.inert = true;
  Object.assign(ghost.style, {
    position: "fixed",
    top: `${b.top}px`,
    left: `${b.left}px`,
    width: `${b.width}px`,
    height: `${b.height}px`,
    margin: "0",
    zIndex: "30",
    pointerEvents: "none",
    transformOrigin: "50% 50%",
    // Leaves are transparent inside their card; give the ghost a body so live cards
    // sliding underneath do not show through the fading copy.
    background: "var(--surface, var(--card, Canvas))",
  } satisfies Partial<CSSStyleDeclaration>);
  document.body.appendChild(ghost);
  return ghost;
}

export type ListMutation = (removedIds: readonly string[], commit: () => void) => void;

/**
 * Returns `mutate(removedMotionIds, commit)`. `commit` performs the React state
 * updates; it is flushed synchronously. Pass `[]` for removed ids when the commit
 * only restores/adds/reorders (anchoring + FLIP + enter still apply).
 */
export function useListMotion(rootRef: RefObject<HTMLElement | null>): ListMutation {
  const live = useRef(new Set<Animation>());
  const ghosts = useRef(new Set<HTMLElement>());

  useEffect(() => {
    const animations = live.current;
    const layer = ghosts.current;
    return () => {
      animations.forEach((a) => a.cancel());
      animations.clear();
      layer.forEach((g) => g.remove());
      layer.clear();
    };
  }, []);

  return useCallback(
    (removedIds, commit) => {
      const root = rootRef.current;
      if (!root || !root.children.length) {
        flushSync(commit);
        return;
      }
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const vh = window.innerHeight;
      const track = (animation: Animation) => {
        live.current.add(animation);
        const done = () => live.current.delete(animation);
        animation.finished.then(done, done);
      };

      // 1. SNAPSHOT — visual positions (include any motion still in flight)…
      const visual = snapshot(root, vh);
      const beforeIds = new Set(
        Array.from(root.querySelectorAll<HTMLElement>("[data-motion-id]"), (n) => n.dataset.motionId!),
      );
      // …and pure layout positions (in-flight animations settled) for scroll anchoring.
      let layout = visual;
      if (live.current.size) {
        live.current.forEach((a) => a.cancel());
        live.current.clear();
        layout = snapshot(root, vh);
      }

      // 2. GHOST — removed nodes leave layout right now and fade out on their own.
      const gone = new Set(removedIds);
      if (!reduced) {
        for (const id of gone) {
          const snap = visual.get(id);
          if (!snap || !inView(snap.box, vh)) continue;
          if (snap.parent && gone.has(snap.parent)) continue; // covered by its slot's ghost
          const ghost = makeGhost(snap.el, snap.box);
          ghosts.current.add(ghost);
          const cleanup = () => {
            ghost.remove();
            ghosts.current.delete(ghost);
          };
          ghost
            .animate(
              [
                { opacity: 1, transform: "scale(1)" },
                { opacity: 0, transform: "scale(0.94)" },
              ],
              { duration: EXIT_MS, easing: "ease-out", fill: "forwards" },
            )
            .finished.then(cleanup, cleanup);
        }
      }

      // Position of the earliest removal in DOM order (−1: far above the viewport, ∞: far below).
      const order = new Map<string, number>();
      let cursor = 0;
      for (const id of layout.keys()) order.set(id, cursor++);
      let firstGone = Infinity;
      for (const id of gone) {
        let at = order.get(id);
        if (at === undefined) {
          const el = root.querySelector<HTMLElement>(`[data-motion-id="${CSS.escape(id)}"]`);
          if (el) at = bottomOf(boxOf(el)) <= 0 ? -1 : Infinity;
        }
        if (at !== undefined) firstGone = Math.min(firstGone, at);
      }

      // 3. COMMIT — synchronous DOM update; no paint has happened since the snapshot.
      flushSync(commit);
      const els = new Map<string, HTMLElement>();
      root.querySelectorAll<HTMLElement>("[data-motion-id]").forEach((n) => els.set(n.dataset.motionId!, n));

      // 4. ANCHOR — decide how far the page must scroll so visible content does not move.
      //   • something visible sits BEFORE the earliest removal → it cannot be affected: no scroll,
      //     the content after the hole simply slides up;
      //   • otherwise (removal is above everything visible) → follow the median shift of the
      //     visible survivors, so one card wrapping to the previous row cannot drag the rest;
      //   • no removals at all (merge / restore) → classic scroll anchoring on the first visible leaf.
      const shifts: number[] = [];
      let stable = false;
      for (const snap of layout.values()) {
        if (!snap.leaf || gone.has(snap.id) || (snap.parent && gone.has(snap.parent))) continue;
        if (!inView(snap.box, vh)) continue;
        const el = els.get(snap.id);
        if (!el) continue;
        if (gone.size && order.get(snap.id)! < firstGone) {
          stable = true;
          break;
        }
        shifts.push(el.getBoundingClientRect().top - snap.box.top);
        if (!gone.size) break;
      }
      if (!stable && shifts.length) {
        shifts.sort((x, y) => x - y);
        const mid = shifts.length >> 1;
        const delta = shifts.length % 2 ? shifts[mid] : (shifts[mid - 1] + shifts[mid]) / 2;
        if (Math.abs(delta) >= 0.5) {
          window.scrollBy({ top: delta, left: 0, behavior: "instant" as ScrollBehavior });
        }
      }
      if (reduced) return;

      // 5. FLIP — read final positions once (after the scroll adjustment), then animate.
      const after = new Map<string, Box>();
      for (const id of visual.keys()) {
        const el = els.get(id);
        if (el) after.set(id, boxOf(el));
      }
      for (const snap of visual.values()) {
        const now = after.get(snap.id);
        if (!now) continue;
        let dx = snap.box.left - now.left;
        let dy = snap.box.top - now.top;
        if (snap.parent) {
          const was = visual.get(snap.parent)?.box;
          const is = after.get(snap.parent);
          if (!was || !is) continue;
          // Parent-relative: the parent's own slide (if any) already carries this node.
          dx -= was.left - is.left;
          dy -= was.top - is.top;
        }
        if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) continue;
        track(
          // offset 0 = start here; the implicit end keyframe is the element's own resting style.
          els.get(snap.id)!.animate([{ offset: 0, transform: `translate(${dx}px, ${dy}px)` }], {
            duration: moveMs(Math.hypot(dx, dy)),
            easing: EASE,
          }),
        );
      }

      // 6. ENTER — outermost nodes that did not exist before fade in (rollback / merge).
      for (const [id, el] of els) {
        if (beforeIds.has(id)) continue;
        const parent = parentIdOf(el);
        if (parent && !beforeIds.has(parent)) continue;
        if (!inBand(boxOf(el), vh)) continue;
        track(
          el.animate([{ offset: 0, opacity: 0, transform: "scale(0.96)" }], {
            duration: ENTER_MS,
            easing: EASE,
          }),
        );
      }
    },
    [rootRef],
  );
}
