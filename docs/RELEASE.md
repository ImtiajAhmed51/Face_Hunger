# Signed-release checklist

Work top to bottom. Nothing here is automated on purpose: a release is a deliberate act.

## 1. Freeze
- [ ] `main` is green: every command in docs/OPERATIONS.md "Checks before a release".
- [ ] Perf suite run on the reference machine; numbers appended to docs/PERFORMANCE.md.
- [ ] `CHANGELOG.md` and `docs/RELEASE_NOTES_v<version>.md` written; version bumped in
      `pyproject.toml`, `backend/app.py` and `frontend/package.json`.

## 2. Upgrade safety
- [ ] `tests/test_upgrade.py` passes from the Phase 1 and Phase 2 fixture data folders.
- [ ] If the schema changed: a fixture data folder from the *previous release* was added
      (`scripts/make_upgrade_fixture.py`, run inside a checkout of that release).
- [ ] Manual: copy a real data folder, start the new version on the copy, confirm the automatic
      backup in `backups/` and spot-check people, albums and search.

## 3. Licences and supply chain
- [ ] Regenerate `requirements.lock` from the tested environment; review the diff.
- [ ] Re-run the licence audit (docs/LICENSES.md); resolve every "Needs attention" row.
      **Open for 1.0:** insightface pin (0.7.3) vs tested version (2.0, no declared licence);
      project `LICENSE` file.
- [ ] `npm audit --omit=dev` and `pip-audit -r requirements.lock` reviewed (run on a networked
      machine; the app itself never does this).
- [ ] No model weights in the repository, the wheel or the image.

## 4. Security
- [ ] `tests/test_security.py`, `tests/test_plugins.py`, `tests/test_packages.py` green.
- [ ] docs/SECURITY.md updated for anything new that parses untrusted input.

## 5. Build reproducibly
- [ ] Pin the base images by digest in `Dockerfile` (`node:...@sha256:...`, `python:...@sha256:...`).
- [ ] `export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)`
- [ ] Build twice on a clean builder; the image digests must match:
      `docker build --no-cache --build-arg SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH -t face-hunger:$V .`
- [ ] Smoke test the image: health check passes, a small library indexes, app lock works.
- [ ] `python -m build` for the sdist/wheel; `(cd frontend && npm ci && npm run build)` output
      matches the image's `frontend/dist` file list.

**Status for 1.0:** the Dockerfile is written but has not been built: Docker was not available in
the development environment. Treat the first build as part of this checklist.

## 6. Sign
- [ ] Tag: `git tag -s v$V -m "Face Hunger $V"`; verify with `git tag -v v$V`.
- [ ] Checksums: `sha256sum dist/* > SHA256SUMS`; sign: `gpg --detach-sign --armor SHA256SUMS`
      (or `minisign -S -m SHA256SUMS`).
- [ ] Image: `cosign sign --key cosign.key <registry>/face-hunger@<digest>`; attach an SBOM
      (`syft <image> -o spdx-json`) with `cosign attest`.
- [ ] Publish the public key fingerprint in the release notes.

## 7. Publish and verify
- [ ] Push the tag; attach artifacts, `SHA256SUMS`, its signature and the SBOM.
- [ ] On a second machine: verify the signature and checksums, install from the artifacts, run the
      upgrade on a copy of a real data folder.
- [ ] Keep the previous release's artifacts available for rollback.
