# Responsive view audit — October 11, 2026

The application remained running at `http://127.0.0.1:8765/`. Live checks only opened views, filters, menus and dialogs; they did not submit identity, library, export or cleanup operations. Interactive mutation regression tests used the existing disposable Playwright library.

## Coverage

| Viewport | Size |
|---|---|
| Desktop | 1440 × 900 |
| Tablet | 820 × 1180 |
| Mobile | 390 × 844 |
| Small mobile | 320 × 740 |

All 24 top-level routes were opened at each size: Photos, Search, People, Albums, Favorites, Recently Added, Videos, Events, Places, Face Review, Face Clusters, Libraries, Settings, Overview, Media Browser, No Faces, Deleted, Duplicates, Cleanup, Storage, Health, Plugins, Diagnostics and Timeline. Available live person/event detail pages brought the live route audit to **104 views**. There were no detected page exceptions, HTTP 5xx responses or document horizontal overflows in that run.

An additional **76 live nested views** covered the four person profile modes, rename/merge/encrypted-export dialogs, person action menu, move-media dialog, expanded search filters, review person picker, folder chooser, network-share form and all six Cleanup categories. Their dialogs stayed within the viewport at each size. Screenshots were inspected; automated geometry checks alone were insufficient to catch content clipped inside a container.

The fixture suite additionally exercises photo/video viewers, album and smart-collection details, face selection and move/split, search person picker, and the full mobile navigation sheet at every size. The existing suite covers navigation, keyboard controls, accessibility, search, review/reassignment/merge/split/undo, library forms and backup interactions. Fixture video coverage does not imply that every real source file is available on the mounted media volume.

## Fixes

- Timeline year labels previously overlapped when several years occupied a short section of the date rail. Labels now respect the measured rail height and a 22px separation while retaining the full set of keyboard/drag navigation anchors. ResizeObserver is cleaned up on unmount. Real-library checks confirmed non-overlapping labels at all four widths.
- Person video-moment rows previously clipped long filenames and timestamp buttons on narrow screens. Their content now shrinks within the flex row, filenames wrap, and timestamp chips wrap onto additional rows. Real-library checks confirmed all names and buttons fit within their rows at all four widths.
- Overview's six-digit library count wrapped onto two lines on small phones and tablets. Mobile statistic cards now use balanced padding and a smaller responsive number size; tablets use two columns. The new large-count regression reproduces the tablet failure before that adjustment.
- A legacy tablet rule hid sidebar labels while the current theme kept the expanded 232px width. Expanded sidebar labels and branding are now visible; deliberate collapse still hides them and uses the 76px rail.
- The previous mobile bottom-navigation centering fix remains in place. No new dependencies or changes to original media were required.

## Verification

Production TypeScript/Vite build passed, with the existing bundle-size warning. Frontend unit tests: **76 passed**. Complete browser suite: **42 passed, 1 skipped** (opt-in Lighthouse). This includes four responsive tests covering every route and the nested views listed above, plus keyboard and light/dark accessibility checks. After the final statistic-card and tablet-sidebar changes, **all six affected browser tests passed**: the complete four-size route/nested-view checks (including large counts and sidebar expand/collapse), Photos interactions and phone accessibility. Live checks also confirmed real library counts fit on one line at all four widths and expanded/collapsed tablet navigation behaves correctly.

The first audit selector had matched the mobile navigation opener instead of a video tile; it was corrected to target the main content. Another run was interrupted after a concurrent production build invalidated cached chunk URLs; the complete successful run used a stable build. A closed native details menu produced a false layout warning from hidden elements; the audit now checks those controls only when the menu is open. Actual open-menu positioning passed at every size.

Final live route checks found no visible horizontal overflow, page exceptions or HTTP 5xx responses in 104 route views. Eight additional live photo/video viewer checks confirmed the dialogs fit the viewport. Loading a real video file successfully is separate from verifying its viewer layout; unavailable mounted originals remain an environment limitation. Production media/face/person counts and complete embedding/manual-assignment/person-name hashes matched the pre-audit snapshot.

Live screenshots and geometry records are in `/tmp/fh-responsive-audit`; fixture screenshots are in `frontend/test-results/responsive`. These are local audit outputs and may be cleared by normal temporary-file cleanup.
