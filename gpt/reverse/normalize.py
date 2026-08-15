from __future__ import annotations

import re
from typing import Any

from gpt.reverse.redact import Redactor, default_redactor

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def normalize_value(val: Any, redactor: Redactor | None = None) -> Any:
    r = redactor or default_redactor
    if isinstance(val, dict):
        return {k: normalize_value(v, r) for k, v in val.items()}
    elif isinstance(val, list):
        return [normalize_value(item, r) for item in val]
    elif isinstance(val, str):
        return _UUID_RE.sub(lambda match: r.get_symbol(match.group(0), "UUID"), val)
    return val


def normalize_trace(
    events: list[dict[str, Any]],
    redactor: Redactor | None = None,
) -> list[dict[str, Any]]:
    """Return a normalized copy of events where UUIDs and entities have deterministic symbols."""
    r = redactor or Redactor()
    normalized = []
    for ev in events:
        redacted = r.redact_event(ev, normalize_ids=True)
        normalized.append(normalize_value(redacted, r))
    return normalized
