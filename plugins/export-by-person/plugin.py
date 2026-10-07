"""Export plan: ``<Person>/<Year>/<file name>`` for every person in a photo.

The contract:

    plan(items: list[dict], options: dict) -> list[{"id": int, "path": str}]

Each item has ``id``, ``name``, ``kind``, ``captured_at`` and ``people``. Paths are relative
to the folder the user picked. The plugin never touches a file: the app validates every
path and does the copying.
"""

import re

UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean(text: str) -> str:
    return UNSAFE.sub("_", text).strip(" .")[:80] or "_"


def plan(items, options):
    by_year = options.get("by_year", True)
    out = []
    for item in items:
        year = (item.get("captured_at") or "")[:4] or "Undated"
        for person in item.get("people") or ["No people"]:
            parts = [clean(person)] + ([year] if by_year else []) + [clean(item["name"])]
            out.append({"id": item["id"], "path": "/".join(parts)})
    return out
