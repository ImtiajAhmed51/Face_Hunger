# Extension guide

Plugins add models, labels, search signals, export targets and small panels to Face Hunger
without being able to harm the library. Plugin API version: **1.0**.

## How a plugin runs

- A plugin is a folder with a `plugin.toml` manifest and Python code. Installing copies it to
  `data_dir/plugins/<id>/`. Its code is **never imported by the server**.
- Each plugin runs in its own subprocess (`backend/plugins/runner.py`), started on first use, with a
  clean environment (no `LFS_*` variables) and a private JSON-lines channel. A crash, hang, or
  invalid reply fails that one call; the next call starts a fresh process. Three crashes switch
  the plugin off until you enable it again.
- A new plugin is **disabled and has no permissions**. Enabling it and granting permissions are
  separate, explicit steps on the Plugins screen. Only permissions the manifest asks for can be granted.

## Sandbox

| Without a permission | What happens |
| --- | --- |
| (always) | No starting processes, no loading native libraries through `ctypes`, no writes outside its own storage folder, no reads of the app's data folder |
| `files.read` | Cannot open or list anything in your libraries (or elsewhere outside Python and its own folder) |
| `network` | Cannot create sockets or resolve names |
| `storage` | Cannot write anywhere |
| `library.read` | Gets `person-<id>` instead of people's names |

Two layers enforce this: a Python audit hook installed before plugin code loads (all platforms),
and on macOS a kernel sandbox profile (`sandbox-exec`) for the whole process, which also covers
native code. **Limit:** on Linux and Windows only the audit hook applies; a plugin that ships its own
compiled extension module could bypass it. Install plugins you trust, or run the app in the
provided container on those platforms. `os.stat` is not audited, so file existence and size can be
probed outside macOS.

## Manifest

```toml
[plugin]
id = "my-plugin"            # lowercase letters, digits, dashes
name = "My plugin"
version = "1.0.0"
api_version = "1.0"         # major must match the app; minor must not be newer
description = "..."
license = "MIT"
permissions = []            # any of: network, files.read, storage, library.read

[capabilities.embedding]    # one table per extension point
entry = "plugin:Embedder"   # module:attribute inside the plugin folder
model_id = "my-model"
version = "1"
dim = 384
```

An `api_version` the app cannot serve is rejected at install with:
`This plugin needs plugin API 2.0; this app provides 1.0.`

Packages installed with pip can advertise a plugin folder through the entry-point group
`facehunger.plugins` (value: the package that contains `plugin.toml`). They are listed as
"available" and still go through the same install and review steps.

## Extension points

`entry` may be a function, or a class (instantiated once per process) with the named method.

| Capability | Method | Receives | Returns |
| --- | --- | --- | --- |
| `embedding` | `embed(images)` | JPEG previews as `bytes` (longest side <= 384 px) | one vector of `dim` floats per image |
| `classifier` | `classify(images)` | same | per image, a list of `{"label", "score"}` (score 0-1) |
| `search_signal` | `rank(query, items)` | the text query and up to 300 candidates (`id`, `name`, `kind`, `captured_at`, `width`, `height`) | `[{"id", "score"}]` |
| `export` | `plan(items, options)` | items with `id`, `name`, `kind`, `captured_at`, `people` | `[{"id", "path"}]`, paths relative to the chosen folder |
| `ui_panel` | (static files) | `entry = "panel/index.html"` | shown in a sandboxed frame |

- **Embedding models** appear under Health > "Re-index with another model". A plugin model is used
  for search only after you switch to it there, and you can go back at any time.
- **Classifier labels** are stored per plugin and act as the `label` search signal.
- **Search signals** have a 2 second budget per query; a failure only adds a warning to the results.
- **Export**: the plugin never touches files. The app checks every path (relative, no `..`, inside
  the target, outside your libraries and the data folder) and copies originals to **new** files.
- **Panels** are served with `Content-Security-Policy: default-src 'none'; connect-src 'none'; sandbox allow-scripts`
  and embedded with `<iframe sandbox="allow-scripts">`. They talk to the app only by `postMessage`:

  ```js
  parent.postMessage({ fh: 1, id: 1, method: "library.summary" }, "*");
  // reply: { fh: 1, id: 1, ok: true, result: { photos, videos, people } }
  ```
  Methods: `host.info`, `library.summary`, `library.people` (needs `library.read`).

## Reference plugins

- `plugins/color-histogram-embedding`: a custom embedding model with no permissions.
- `plugins/export-by-person`: an export target (`Person/Year/file`) and a panel; asks for `library.read`.

## API

`GET /api/plugins`, `POST /api/plugins/install {path}`, `PATCH /api/plugins/{id} {enabled, permissions}`,
`DELETE /api/plugins/{id}`, `POST /api/plugins/{id}/test`, `POST /api/plugins/{id}/export`,
`POST /api/plugins/{id}/classify`, `GET /api/media/{id}/plugin-labels`,
`GET /api/plugins/{id}/panel/{path}`, `POST /api/plugins/{id}/panel-rpc`.

Uninstalling stops the process and removes the plugin's folder, storage, labels and registry row.
Vectors built by an embedding plugin stay on disk (search falls back to the built-in model).
