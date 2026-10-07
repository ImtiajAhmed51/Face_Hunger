# Colour histogram embedding (reference plugin)

Shows the **embedding** extension point. Install it from Plugins > Install from a folder, enable it,
then open Health > "Re-index with another model" and choose `plugin-color-histogram-embedding-rgb-hist`.
The index is built beside the current one; compare, switch, or go back.

It asks for **no permissions**. The app sends JPEG previews to the plugin process; the plugin
returns vectors. To plug in your own model, keep `plugin.toml` (change `id`, `model_id`, `dim`)
and replace `Embedder.embed`. Bump `version` under `[capabilities.embedding]` whenever the vectors
change: a new version gets a new index and never overwrites the old one.
