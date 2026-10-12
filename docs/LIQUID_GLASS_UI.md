# Liquid Glass web interface

The frontend uses Tailwind CSS v4 through the Vite plugin. Preflight is omitted to retain existing native media and form sizing. `src/tailwind.css` maps utilities to the app's light/dark tokens.

`src/components/primitives/button.tsx` is adapted from the official shadcn/ui New York Button registry (MIT). All application buttons use this shared Radix Slot/CVA component. Its `legacy` variant preserves image, navigation and specialized control dimensions; standard shadcn variants remain available. Existing native dialogs retain their focus, Escape, nested-modal and scroll-lock handling.

`src/liquid-glass.css` applies translucent surfaces, rounded controls, rim highlights and backdrop blur across the shell, panels, forms, sheets, dialogs and navigation. Blur is kept away from individual library thumbnails to avoid creating thousands of compositing layers. Selection borders remain inside thumbnails. Reduced motion, reduced transparency and forced-colour preferences are respected.

This is a browser interpretation of Apple's published Liquid Glass design, not Apple's native material renderer or a pixel-exact reproduction of iOS 27. It does not add external uploads, runtime CDN assets or automatic video conversion.

Start from the project root: `LFS_HOST=127.0.0.1 .venv/bin/python -m backend`.
Build: `npm --prefix frontend run build`.

The October 12 visual refresh consolidates the duplicate material overrides into the shared light/dark tokens. Chrome uses 24px backdrop blur with restrained translucent fills; dialogs use 36px blur, softer rims and a 320ms entrance. Navigation and segmented controls share capsule shapes and a 180ms/320ms interaction rhythm. Panels use responsive padding; mobile navigation retains safe-area spacing and full-width navigation sheets. Reduced-motion users receive immediate state changes, and reduced-transparency/unsupported-blur environments retain opaque readable surfaces. No new dependency or backend change is needed.

The existing responsive suite checks all routes and nested dialogs at 320, 390, 820 and 1440px; the accessibility suite covers light/dark pages, phone layout and keyboard focus. `e2e/glass-motion.spec.ts` additionally checks translucent navigation, light/dark screenshots, dialog opening/Escape and reduced-motion behavior against a disposable fixture library.
