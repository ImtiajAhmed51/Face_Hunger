# Engineering decisions

Short log of choices and dependencies. One entry per decision, newest last.

## Licensing
- **buffalo_l (InsightFace) weights are non-commercial only.** The code is MIT, but the
  pretrained face models may not be used commercially. New models added by this project are
  Apache-2.0 (SigLIP 2, DINOv2) only.

## Backend structure
- **App factory + routers (`backend/app.py`, `backend/routers/*`).** `backend/__main__.py` only
  builds the default app. Services live in one `Services` container; routers reach them via
  late-bound proxies in `backend/deps.py`, so maintenance actions that reopen the face store are
  visible everywhere without module globals.
- **API contract snapshot.** `tests/fixtures/api_baseline.json` was captured from the monolithic
  `__main__.py` before the split; `tests/test_api_contract.py` fails on any removed or changed
  operation. New endpoints are allowed; changed ones are not.

## Tooling
- **ruff** (dev only): Python lint (`F`, `E9`, import order). Fast, single binary, no runtime cost.
- Frontend "lint" is `tsc -b` in strict mode with `noUnusedLocals/Parameters`; no ESLint, to keep
  the dev toolchain small.
