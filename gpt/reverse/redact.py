from __future__ import annotations

import json
import re
from typing import Any

_SENSITIVE_HEADER_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-csrf-token",
    "csrf-token",
    "x-xsrf-token",
    "xsrf-token",
    "oai-client-token",
    "x-authorization",
    "openai-organization",
    "openai-project",
    "sec-websocket-key",
}

_SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "cookie",
    "credential",
    "session_key",
    "jwt",
)

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:access_token|token|auth|key|signature|sig|__cf_chl_rt_tk|cf_clearance)=)[^&#\s]+",
    re.IGNORECASE,
)


def _sensitive_key(key: str) -> bool:
    key = key.casefold().replace("-", "_")
    return (
        key in {item.replace("-", "_") for item in _SENSITIVE_HEADER_KEYS}
        or any(part in key for part in _SENSITIVE_KEY_SUBSTRINGS)
        or key.endswith(("_token", "_secret", "_authorization"))
        or key.startswith(("token_", "secret_"))
        or key in {"token", "secret", "authorization", "session"}
    )


class Redactor:
    """Masks secret credentials and normalizes dynamic entity IDs."""

    def __init__(self):
        self._symbol_map: dict[str, str] = {}
        self._counters: dict[str, int] = {
            "CONV": 0,
            "MSG": 0,
            "USER": 0,
            "RUN": 0,
            "ID": 0,
            "UUID": 0,
        }

    def reset_symbols(self) -> None:
        self._symbol_map.clear()
        for k in self._counters:
            self._counters[k] = 0

    def get_symbol(self, raw_value: str, prefix: str = "ID") -> str:
        if not raw_value or not isinstance(raw_value, str):
            return raw_value
        if raw_value in self._symbol_map:
            return self._symbol_map[raw_value]

        count = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = count
        symbol = f"<{prefix}_{count}>"
        self._symbol_map[raw_value] = symbol
        return symbol

    def redact_headers(self, headers: dict[str, Any] | list[tuple[str, str]]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        items = headers.items() if isinstance(headers, dict) else headers
        for k, v in items:
            key_str = str(k).lower()
            val_str = str(v)
            if _sensitive_key(key_str):
                normalized[key_str] = "<REDACTED>"
            else:
                redacted_val = _BEARER_RE.sub("Bearer <REDACTED>", val_str)
                redacted_val = _JWT_RE.sub("<JWT_REDACTED>", redacted_val)
                redacted_val = _API_KEY_RE.sub("<KEY_REDACTED>", redacted_val)
                normalized[key_str] = redacted_val
        return normalized

    def redact_string(self, text: str) -> str:
        if not text or not isinstance(text, str):
            return text
        redacted = _BEARER_RE.sub("Bearer <REDACTED>", text)
        redacted = _JWT_RE.sub("<JWT_REDACTED>", redacted)
        redacted = _API_KEY_RE.sub("<KEY_REDACTED>", redacted)
        redacted = _QUERY_SECRET_RE.sub(r"\1<REDACTED>", redacted)
        return redacted

    def redact_json(self, data: Any, normalize_ids: bool = False) -> Any:
        if isinstance(data, dict):
            new_dict = {}
            for k, v in data.items():
                lower_k = str(k).lower()
                if _sensitive_key(lower_k):
                    new_dict[k] = "<REDACTED>"
                elif normalize_ids and isinstance(v, str):
                    if "conversation_id" in lower_k:
                        new_dict[k] = self.get_symbol(v, "CONV")
                    elif "message_id" in lower_k or "turn_id" in lower_k:
                        new_dict[k] = self.get_symbol(v, "MSG")
                    elif "user_id" in lower_k or "account_id" in lower_k:
                        new_dict[k] = self.get_symbol(v, "USER")
                    else:
                        new_dict[k] = self.redact_json(v, normalize_ids=normalize_ids)
                else:
                    new_dict[k] = self.redact_json(v, normalize_ids=normalize_ids)
            return new_dict
        elif isinstance(data, list):
            return [self.redact_json(item, normalize_ids=normalize_ids) for item in data]
        elif isinstance(data, str):
            return self.redact_string(data)
        return data

    def redact_event(self, event_dict: dict[str, Any], normalize_ids: bool = False) -> dict[str, Any]:
        redacted = dict(event_dict)
        if "metadata" in redacted and isinstance(redacted["metadata"], dict):
            meta = dict(redacted["metadata"])
            for hkey in ("headers", "request_headers", "response_headers"):
                if hkey in meta and isinstance(meta[hkey], (dict, list)):
                    meta[hkey] = self.redact_headers(meta[hkey])
            if "post_data" in meta and isinstance(meta["post_data"], str):
                try:
                    parsed = json.loads(meta["post_data"])
                    meta["post_data"] = self.redact_json(parsed, normalize_ids=normalize_ids)
                except Exception:
                    meta["post_data"] = self.redact_string(meta["post_data"])
            redacted["metadata"] = meta

        # Process fields without double-redacting
        return self.redact_json(redacted, normalize_ids=normalize_ids)


default_redactor = Redactor()
