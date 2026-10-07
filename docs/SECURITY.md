# Security review (v1.0)

Scope: the server, the web app, import/restore paths and the plugin sandbox. Threat model: a
personal app on a home network. Attackers considered: a malicious web page in the user's browser, another
device on the network, a malicious file given to the user (backup, package, plugin, photo), and a
malicious plugin. Not in scope: an attacker with the user's OS account, or exposure to the internet.

Every row is covered by `tests/test_security.py` unless another file is named.

| Area | Finding in this pass | State |
| --- | --- | --- |
| CSRF | The `X-LFS-Request` header was checked inside each handler, after body validation, so a malformed request got 422 before the check. | **Fixed**: enforced in middleware for every POST/PUT/PATCH/DELETE under `/api`; a test enumerates all routes. |
| Cross-origin | No Origin check; CORS was the only barrier. | **Fixed**: state-changing requests with a foreign or `null` Origin are refused. |
| DNS rebinding | Any Host header was accepted. | **Fixed**: Host must be an IP, `localhost`, or listed in `LFS_ALLOWED_HOSTS`. |
| Auth / app lock | None existed; the default bind is all interfaces. | **Added**: optional password (scrypt hash, or `LFS_APP_PASSWORD`), HttpOnly SameSite=Strict session cookie, lockout after 5 failures per minute, all `/api` routes including media closed while locked. Default bind unchanged for compatibility; the startup warning and docs/PRIVACY.md explain it. |
| Path traversal | The SPA fallback joined the URL path to the frontend folder without a containment check: `/..%2f<file>` could read files outside it. | **Fixed** (resolve + `is_relative_to`). Package downloads, plugin panels and library roots were already contained; all are tested with encoded variants. |
| Zip-slip (backup restore) | Names were checked for `..` and absolute paths only; any other relative path was accepted. | **Fixed**: only the files a backup can contain are accepted (`index.sqlite`, the two vector files, `vectors/*.f32`, `thumbnails/*.jpg`). |
| Decompression bomb (backup restore) | Entries were hashed and extracted without size limits. | **Fixed**: declared size must equal the zip entry size; implausible compression ratios are refused; free space is checked before extracting. |
| Package import (`.fhpack`) | Already strict: authenticated chunks, allow-listed member names, no links, size bounded by the package size, bounded KDF parameters. | Verified (`tests/test_packages.py`), plus garbage and oversized-header fixtures here. |
| SSRF | The backend has no HTTP client and no endpoint that takes a URL. | Verified by a code scan and an OpenAPI scan. |
| Plugin sandbox | See docs/EXTENDING.md. Subprocess, audit hook, macOS kernel sandbox, host-validated export paths, CSP-sandboxed panels. | Verified (`tests/test_plugins.py`). **Known limit:** native extension modules on Linux/Windows. |
| XSS / content | No `dangerouslySetInnerHTML`; CSP `script-src 'self'`; `nosniff`, `frame-ancestors 'self'`, `Referrer-Policy: no-referrer`. | Verified (Playwright runs fail on any CSP violation). |
| Secrets | Package passphrases are held in memory for one job and never written; the app password is stored only as a salted scrypt hash; plugins get a clean environment. | Verified. |

## Known limitations

- No TLS. On a network you do not trust, put the app behind a TLS-terminating proxy or keep it on
  `127.0.0.1`.
- One shared password, no user accounts, no per-user permissions.
- Sessions live in memory: restarting the server signs everyone out.
- The default bind address is still all interfaces (unchanged from earlier versions).
- Image and video decoding relies on Pillow, OpenCV, libheif, LibRaw and ffmpeg; keep them updated.
