from __future__ import annotations

from typing import Any


def diff_json(a: Any, b: Any, path: str = "$") -> dict[str, dict[str, Any]]:
    """Compute structural differences between two JSON structures with field path tracking."""
    diffs: dict[str, dict[str, Any]] = {}

    if type(a) is not type(b):
        diffs[path] = {"a": a, "b": b, "type_mismatch": True}
        return diffs

    if isinstance(a, dict):
        all_keys = set(a.keys()).union(set(b.keys()))
        for k in sorted(all_keys):
            subpath = f"{path}.{k}"
            if k not in a:
                diffs[subpath] = {"a": "<MISSING>", "b": b[k]}
            elif k not in b:
                diffs[subpath] = {"a": a[k], "b": "<MISSING>"}
            else:
                subdiffs = diff_json(a[k], b[k], subpath)
                diffs.update(subdiffs)
    elif isinstance(a, list):
        max_len = max(len(a), len(b))
        for i in range(max_len):
            subpath = f"{path}[{i}]"
            if i >= len(a):
                diffs[subpath] = {"a": "<MISSING>", "b": b[i]}
            elif i >= len(b):
                diffs[subpath] = {"a": a[i], "b": "<MISSING>"}
            else:
                subdiffs = diff_json(a[i], b[i], subpath)
                diffs.update(subdiffs)
    else:
        if a != b:
            diffs[path] = {"a": a, "b": b}

    return diffs


def classify_field(
    field_path: str,
    runs: list[Any],
    variables: list[str],
) -> str:
    """Classifies a JSON field across multiple runs based on variance."""
    if not runs:
        return "UNKNOWN"

    values = []
    for r in runs:
        val = r
        parts = field_path.replace("$", "").strip(".").split(".")
        found = True
        for p in parts:
            if not p:
                continue
            if isinstance(val, dict) and p in val:
                val = val[p]
            else:
                found = False
                break
        if found:
            values.append(val)

    if not values:
        return "MISSING"

    # If field name clearly denotes semantics
    varies = not all(v == values[0] for v in values)
    if ("prompt" in field_path.lower() or "content" in field_path.lower()) and varies:
        return "CONTENT_DEPENDENT"
    if "model" in field_path.lower() and varies:
        return "MODEL_DEPENDENT"
    if ("conversation" in field_path.lower() or "conv" in field_path.lower()) and varies:
        return "PER_CONVERSATION"

    if all(v == values[0] for v in values):
        return "CONSTANT"

    return "PER_RUN"
