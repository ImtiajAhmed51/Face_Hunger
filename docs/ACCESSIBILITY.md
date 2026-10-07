# Accessibility audit (WCAG 2.2 AA)

Audited on 2026-10-08. Automated checks run in CI-style tests; manual checks are listed with what
was and was not done.

## Automated (frontend/e2e/a11y.spec.ts, axe-core via Playwright)

- Rule sets: WCAG 2.0 A/AA, 2.1 A/AA and 2.2 AA.
- Screens: all 23 routes (home, people, person, clusters, photos, videos, no-faces, deleted,
  search, review, duplicates, cleanup, settings, health, storage, plugins, diagnostics, timeline,
  events, event, albums, favourites, map), each in **light and dark** colour schemes.
- Eight core screens again at **375 px** width, plus a check for horizontal scrolling (1.4.10 Reflow).
- Every screen: exactly one `h1`, one `main` landmark, a page title, a `lang` attribute, and a
  visible focus indicator on keyboard focus.
- Result: **0 violations**.

## Found and fixed in this audit

| Criterion | Where | Fix |
| --- | --- | --- |
| 2.5.8 Target Size (Minimum) | Events: the select checkbox on each card was smaller than 24 x 24 px | checkbox enlarged to 24 px |
| 1.4.10 Reflow | Home at 375 px: the memories strip pushed the page 51 px wider than the screen | grid track constrained so the strip scrolls inside itself |

## Covered by other tests

- Keyboard-only flows: duplicates resolver, timeline scrubber (`e2e/screens.spec.ts`).
- Reduced motion: no running animations with `prefers-reduced-motion` (`e2e/screens.spec.ts`).
- Language: English and Bangla; untranslated strings in screens fail `src/i18n.test.ts`.

## Not covered by automation (manual review still recommended)

- Screen-reader walkthroughs (VoiceOver/NVDA) of the viewer, edit tools and share dialog.
- 1.4.4 at 200% text zoom and 1.4.12 text spacing on every screen (spot-checked on core screens only).
- Plugin panels: third-party content inside the frame is the plugin author's responsibility; the
  frame itself has a title.
- Photo content has no text alternative beyond file name, people and optional captions.

Lighthouse scores for the core screens are in docs/PERFORMANCE.md.
