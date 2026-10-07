# Privacy statement

Face Hunger is a local application. This page says exactly what it does with your data.

## What stays on your device

Everything. Your photos and videos, the face and image index, people's names, albums, edits,
captions, thumbnails, backups, plugin data and diagnostics are files under the data folder
(`LFS_DATA_DIR`) on your computer, or next to your originals (`.xmp` sidecars).

## What the app sends over the network

Nothing. The server contains no HTTP client and opens no outbound connections (enforced by a test
that scans the code, and by tests that count sockets). There is no telemetry, no crash reporting,
no update check, no CDN: the web page is served by the app itself, and its content security policy
forbids loading anything from elsewhere.

Two things use the network, and only when **you** do them:

- `python scripts/fetch_models.py` downloads the optional models once, from Hugging Face. It is a
  separate script that you run by hand; the app never runs it.
- The map screen shows a base map only if you install a map file yourself. By default it draws
  your photos on a blank map and downloads nothing.

## Who can reach the app

By default it listens on your network interface (`LFS_HOST`, default `0.0.0.0`) so other devices of
yours can use it. If you do not need that, set `LFS_HOST=127.0.0.1`. If you do, turn on the
**app lock** (Settings, or `LFS_APP_PASSWORD`); without it, anyone on your network who can reach
the port can browse the library. The app warns about this at startup and on the Health screen.
It has no user accounts and is not designed to be exposed to the internet.

## Your originals

The app never modifies your original files. Edits are stored in sidecars and the database.
"Delete" moves an item to Deleted inside the app; freeing space moves originals into a bin folder
in the data folder, from which undo restores them byte for byte.

## Things you export on purpose

- **Share safely** writes new files with faces anonymised and metadata removed.
- **Encrypted packages** (`.fhpack`) are encrypted with your passphrase (scrypt + AES-256-GCM). The
  passphrase is never stored.
- **Backups** are plain zip files of the index. Keep them as private as the library.
- **Diagnostics** are off by default. If you turn them on, timings are stored locally; the report
  you can export contains operation names and numbers only (no paths, file names, people or search
  text) and is never sent anywhere by the app.

## Models and plugins

Models run on your device. The optional assistant model is never loaded unless you enable it.
Plugins run in a separate sandboxed process, start disabled with no permissions, and cannot use the
network or read your files unless you grant that plugin the permission (see docs/EXTENDING.md for
the limits of the sandbox on Linux and Windows).

## Faces

The app computes face embeddings (biometric data) for the people in your photos and stores them
locally. Depending on where you live, processing other people's biometric data may be regulated
even for personal use; that responsibility is yours.
