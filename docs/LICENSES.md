# License audit (v1.0)

Audited on 2026-10-08 from the installed package metadata (`importlib.metadata`,
`node_modules/*/package.json`) and `scripts/fetch_models.py`. Re-run before each release
(docs/RELEASE.md, step 3).

**Result:** every runtime dependency is MIT, BSD, Apache-2.0 or an equivalent permissive licence,
with three items that need attention (below). The project itself has no licence file yet.

## Needs attention

| Item | Finding | What to do |
| --- | --- | --- |
| `insightface` | `pyproject.toml` pins `0.7.3` (MIT code), but the environment these tests ran in has **2.0**, whose package metadata declares **no licence**. | Before distributing: either install the pinned 0.7.3 and re-run the `ai` tests, or confirm the 2.0 licence upstream and update the pin. `requirements.lock` records what was tested (2.0). |
| `buffalo_l` face models | Weights are published for **non-commercial research use only**. | Not shipped and not downloaded by `scripts/fetch_models.py`. The user installs them. Do not bundle them in an image or installer; do not use the app commercially with them. |
| Project licence | README says "Personal / private use"; there is no `LICENSE` file. | Add one before publishing. |

Also note: `rawpy` (optional, MIT) links LibRaw (LGPL-2.1 / CDDL) dynamically; `ffmpeg` is a system
tool called as a separate program (LGPL/GPL depending on the build) and is not bundled outside the
container image; `@axe-core/playwright` (MPL-2.0) and `lighthouse` (Apache-2.0) are test tools only.

## Python runtime dependencies

| Package | Tested version | Licence |
| --- | --- | --- |
| fastapi | 0.141.1 | MIT |
| uvicorn | 0.52.4 | BSD-3-Clause |
| pydantic-settings | 2.6.1 | MIT |
| numpy | 2.1.3 | BSD-3-Clause |
| opencv-python-headless | 5.0.0.93 | Apache-2.0 |
| Pillow | 11.3.0 | MIT-CMU (HPND) |
| pillow-heif | 1.7.0 | BSD-3-Clause (bundles libheif, LGPL-3.0, dynamically linked) |
| pillow-avif-plugin | 1.6.0 | MIT (bundles libavif, BSD-2-Clause) |
| scikit-learn | 1.6.1 | BSD-3-Clause |
| onnxruntime | 1.30.0 | MIT |
| insightface | 2.0 (pin: 0.7.3) | see "Needs attention" |
| python-multipart | 0.0.32 | Apache-2.0 |
| usearch | 2.26.2 | Apache-2.0 |
| tokenizers | 0.23.2 | Apache-2.0 |
| watchdog | 4.0.2 | Apache-2.0 |
| cryptography | 44.0.1 | Apache-2.0 OR BSD-3-Clause |
| rawpy (optional) | 0.27.1 | MIT |
| psutil (optional, memory readings) | 5.9.0 | BSD-3-Clause |
| faiss-cpu (optional, not installed) | - | MIT |

Development only: pytest (MIT), httpx (BSD-3-Clause), ruff (MIT).
Transitive packages are pinned in `requirements.lock`; none was found with a copyleft licence
by a metadata scan, but that scan is only as good as the metadata (see insightface).

## JavaScript

| Package | Version | Licence | Shipped to the browser |
| --- | --- | --- | --- |
| react, react-dom | 19.1.1 | MIT | yes |
| react-router-dom | 7.18.3 | MIT | yes |
| @tanstack/react-query | 5.104.0 | MIT | yes |
| @tanstack/react-virtual | 3.14.13 | MIT | yes |
| maplibre-gl | 5.24.0 | BSD-3-Clause | yes (map screen only) |
| pmtiles | 4.5.0 | BSD-3-Clause | yes (map screen only) |
| vite, vitest, @vitejs/plugin-react | 6.4.3 / 4.1.11 / 4.5.2 | MIT | no |
| typescript, @playwright/test | 5.8.3 / 1.63.0 | Apache-2.0 | no |
| @axe-core/playwright | 4.13.0 | MPL-2.0 | no |
| lighthouse | 12.x | Apache-2.0 | no |

## Models

| Model | Licence | Shipped | Fetched by `scripts/fetch_models.py` |
| --- | --- | --- | --- |
| SigLIP 2 base (text/image search) | Apache-2.0 | no | yes (default) |
| DINOv2 small / base (visual similarity) | Apache-2.0 | no | yes (small by default) |
| SmolVLM2-500M (optional assistant) | Apache-2.0 | no | only with `--only smolvlm2-500m` |
| buffalo_l (faces) | non-commercial research only | no | **no** (manual, user's decision) |

## Reference plugins

`plugins/color-histogram-embedding`, `plugins/export-by-person`: MIT, no dependencies beyond the app's.
