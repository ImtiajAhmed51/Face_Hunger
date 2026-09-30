"""The refactor into routers must not change the public API surface."""

import json
from pathlib import Path

from backend.app import iter_routes

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "api_baseline.json").read_text())


def _snapshot(app):
    spec = app.openapi()
    ops = {}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            body = op.get("requestBody")
            ops[f"{method.upper()} {path}"] = {
                "params": sorted([p["name"], p["in"], p.get("required", False)] for p in op.get("parameters", [])),
                "body": json.dumps(body["content"]["application/json"]["schema"]) if body else None,
            }
    return ops


def test_openapi_operations_are_unchanged(client):
    current = _snapshot(client.app)
    baseline = BASELINE["operations"]
    missing = sorted(set(baseline) - set(current))
    assert not missing, f"endpoints removed: {missing}"
    for key, spec in baseline.items():
        got = current[key]
        old_params = [list(p) for p in spec["params"]]
        # Existing parameters keep name/location/requiredness; only optional ones may be added.
        assert all(p in got["params"] for p in old_params), key
        assert all(not p[2] for p in got["params"] if p not in old_params), key
        assert (spec["body"] is None) == (got["body"] is None), key


def test_route_table_is_a_superset_of_baseline(client):
    current = {(r.path, tuple(sorted(getattr(r, "methods", None) or []))) for r in iter_routes(client.app.routes)}
    baseline = {(r["path"], tuple(r["methods"])) for r in BASELINE["routes"]}
    assert baseline <= current, sorted(baseline - current)


def test_request_schemas_keep_their_fields(client):
    schemas = client.app.openapi()["components"]["schemas"]
    for name, fields in BASELINE["schemas"].items():
        assert name in schemas, name
        assert set(fields) <= set(schemas[name].get("properties", {})), name
        # New fields must be optional so existing clients keep working.
        required = set(schemas[name].get("required", []))
        assert required <= set(fields), (name, required - set(fields))


def test_static_media_routes_precede_parameterised_ones(client):
    paths = [r.path for r in iter_routes(client.app.routes)]
    assert paths.index("/api/media/soft-originals") < paths.index("/api/media/{media_id}")
    assert paths.index("/api/media/conversion-statuses") < paths.index("/api/media/{media_id}")
    assert paths[-1] == "/{full_path:path}"
