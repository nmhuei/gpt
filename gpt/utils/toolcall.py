from __future__ import annotations

import ast
import base64
import json
import os
import re
import uuid
from pathlib import PurePosixPath
from typing import Any

import tomllib

from gpt.state import MalformedToolCall

_OPEN = "<WEBGPT_TOOL_CALL>"
_CLOSE = "</WEBGPT_TOOL_CALL>"
_BLOCK_RE = re.compile(r"<WEBGPT_TOOL_CALL>\s*([\s\S]*?)\s*</WEBGPT_TOOL_CALL>")
_DSML_OPEN = "<|DSML|tool_calls>"
_DSML_CLOSE = "</|DSML|tool_calls>"
_DSML_BLOCK_RE = re.compile(r"<\|DSML\|tool_calls>\s*([\s\S]*?)\s*</\|DSML\|tool_calls>")
_DSML_INVOKE_RE = re.compile(r"<\|DSML\|invoke\s+name=\"([^\"]+)\">\s*([\s\S]*?)\s*</\|DSML\|invoke>")
_DSML_PARAM_RE = re.compile(r"<\|DSML\|parameter\s+name=\"([^\"]+)\">\s*([\s\S]*?)\s*</\|DSML\|parameter>")
_XML_OPEN = "<tool_calls>"
_XML_CLOSE = "</tool_calls>"
_XML_BLOCK_RE = re.compile(r"<tool_calls>\s*([\s\S]*?)\s*</tool_calls>")
_XML_INVOKE_RE = re.compile(r"<invoke\s+name=\"([^\"]+)\">\s*([\s\S]*?)\s*</invoke>")
_XML_PARAM_RE = re.compile(r"<parameter\s+name=\"([^\"]+)\">\s*([\s\S]*?)\s*</parameter>")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

# json-fn protocol variant: OpenAI function-calling style ```json fences or
# bare JSON payloads carrying [{"name": ..., "arguments": {...}}, ...].
_JSON_FENCE_RE = re.compile(
    r"```[ \t]*json(?![A-Za-z0-9_-])[ \t]*\r?\n?([\s\S]*?)```", re.IGNORECASE
)
_JSON_FENCE_OPEN_RE = re.compile(r"```[ \t]*json(?![A-Za-z0-9_-])", re.IGNORECASE)
_TOOL_PROTOCOL_ENV = "WEBGPT_TOOL_PROTOCOL"
_TOOL_PROTOCOLS = ("xml", "json-fn", "both", "soft")

# DISCOVER-FIRST POLICY (owner decision, T3 follow-up): appended as a numbered
# RULE in every protocol variant of build_tool_instructions() so the web model
# inspects its workspace instead of bouncing path questions back to the
# controller.  Kept protocol-agnostic on purpose.
_WORKSPACE_POLICY_TEXT = (
    "WORKSPACE POLICY (mandatory):\n"
    "When a task references files, binaries, source code, challenges, or artifacts "
    "without explicitly naming their paths:\n"
    "1. Assume the current working directory is the task workspace.\n"
    "2. Inspect the current directory BEFORE asking the controller for paths.\n"
    "3. Use filesystem tools (ls/find/cat) to discover likely relevant files.\n"
    "4. Read metadata/README/challenge description files first.\n"
    "5. Ask ONLY if discovery fails or targets are genuinely ambiguous.\n"
    "If information is missing: obtainable via your tools -> obtain it yourself; "
    "only the controller knows it -> then ask.\n"
    "Workflow: DISCOVER (pwd/ls/find) -> INSPECT (file/strings/README) -> "
    "ANALYZE -> ACT -> VERIFY. Never claim an action is done without running "
    "the verifying command."
)


_VIRTUAL_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "Write",
        "description": "create or replace a text file with exact content; gateway translates this to the client shell tool",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Relative path inside the client workspace"},
                "content": {"type": "string", "description": "Exact UTF-8 file content for non-code text"},
                "lines": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": "Indentation-safe content as JSON array or indent-coded text lines like 4|return x; required for Python/source files",
                },
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
    },
}


def _has_tool(tools: list[dict[str, Any]], name: str) -> bool:
    return any(
        isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == name
        for tool in tools
    )


def _shell_tool_name(tools_or_definitions: Any) -> str | None:
    if isinstance(tools_or_definitions, dict):
        names = set(tools_or_definitions)
    else:
        names = {
            tool.get("function", {}).get("name")
            for tool in tools_or_definitions
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        }
    for candidate in ("Bash", "bash"):
        if candidate in names:
            return candidate
    return None


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\''") + "'"


_SAFE_WRITE_PY = r'''from __future__ import annotations
import ast
import base64
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path

root = Path.cwd().resolve()
requested = Path(sys.argv[1])
target = (root / requested).resolve(strict=False)
try:
    target.relative_to(root)
except ValueError as exc:
    raise SystemExit("WEBGPT_WRITE_REJECTED workspace_escape") from exc

payload = base64.b64decode(sys.argv[2].encode("ascii"), validate=True)
text = payload.decode("utf-8")
suffix = target.suffix.casefold()
if suffix == ".py":
    ast.parse(text, filename=str(requested))
elif suffix == ".json":
    json.loads(text)
elif suffix == ".toml":
    tomllib.loads(text)

target.parent.mkdir(parents=True, exist_ok=True)
fd, temp_name = tempfile.mkstemp(
    prefix=f".{target.name}.webgpt.", suffix=".tmp", dir=str(target.parent)
)
try:
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_name, target)
    try:
        parent_fd = os.open(target.parent, os.O_RDONLY)
    except OSError:
        parent_fd = None
    if parent_fd is not None:
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
finally:
    if os.path.exists(temp_name):
        os.unlink(temp_name)

print(f"WEBGPT_WRITE_OK {sys.argv[1]}")
'''


def _decode_write_lines(value: Any) -> str:
    if isinstance(value, list):
        decoded_list: list[str] = []
        for raw_line in value:
            line = str(raw_line)
            marker = re.fullmatch(r"(\d{1,3})\|(.*)", line, re.DOTALL)
            if marker:
                decoded_list.append(" " * int(marker.group(1)) + marker.group(2))
            else:
                decoded_list.append(line)
        content = "\n".join(decoded_list)
    elif isinstance(value, str):
        raw = value.strip("\n")
        decoded: list[str] = []
        markers = list(re.finditer(r"(\d{1,3})\|", raw))
        # ChatGPT Web can collapse CDATA newlines inside contenteditable, producing
        # strings such as ``0|def f(): 4|return 1``.  Treat every indent marker
        # as a logical line start so virtual Write remains raw-API-like.
        if markers:
            prefix = raw[: markers[0].start()].strip()
            if prefix:
                decoded.extend(prefix.splitlines())
            for index, marker in enumerate(markers):
                next_start = markers[index + 1].start() if index + 1 < len(markers) else len(raw)
                body = raw[marker.end() : next_start]
                body = body.strip("\r\n")
                if index + 1 < len(markers):
                    body = body.rstrip()
                decoded.append(" " * int(marker.group(1)) + body)
        else:
            decoded.extend(raw.splitlines())
        content = "\n".join(decoded)
    else:
        raise MalformedToolCall("Write.lines must be an array or indent-coded string.")
    if not content.endswith("\n"):
        content += "\n"
    return content


def _validate_virtual_write_path(file_path: str) -> None:
    if not file_path or any(ord(char) < 0x20 for char in file_path):
        raise MalformedToolCall("Write.file_path must be a non-empty relative path without control characters.")
    normalized = file_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise MalformedToolCall(
            "Write.file_path must stay inside the client workspace and may not be absolute or contain '..'."
        )


def _validate_write_content(file_path: str, content: str) -> None:
    suffix = PurePosixPath(file_path).suffix.casefold()
    try:
        if suffix == ".py":
            ast.parse(content, filename=file_path)
        elif suffix == ".json":
            json.loads(content)
        elif suffix == ".toml":
            tomllib.loads(content)
        elif suffix in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore[import-untyped]
            except ImportError:
                return
            yaml.safe_load(content)
    except (SyntaxError, json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise MalformedToolCall(
            f"Write content validation failed for {file_path}: {type(exc).__name__}: {exc}"
        ) from exc


def _virtual_write_to_bash(
    arguments: dict[str, Any], *, include_description: bool = True
) -> dict[str, Any]:
    file_path = str(arguments.get("file_path", ""))
    _validate_virtual_write_path(file_path)
    if file_path.casefold().endswith(".py") and "lines" not in arguments:
        raise MalformedToolCall(
            "Write for Python source requires indentation-safe Write.lines; Write.content is not accepted."
        )
    if "lines" in arguments:
        content = _decode_write_lines(arguments["lines"])
    elif isinstance(arguments.get("content"), str):
        content = arguments["content"]
    else:
        raise MalformedToolCall("Write requires either content or lines.")
    _validate_write_content(file_path, content)

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    command = " ".join(
        (
            "python",
            "-c",
            _shell_quote(_SAFE_WRITE_PY),
            _shell_quote(file_path),
            _shell_quote(encoded),
        )
    )
    result = {"command": command}
    if include_description:
        result["description"] = (
            f"Atomically write {file_path} inside the client workspace using the gateway virtual Write adapter"
        )
    return result


def _mask_markdown_code(
    text: str, shield_spans: list[tuple[int, int]] | None = None
) -> str:
    """Mask fenced/inline Markdown code while preserving character offsets.

    Codex13 finding #3 (2026-08-26): ``shield_spans`` lists soft-tag regions
    (<cmd>...</cmd>/<json>...</json>) found on the RAW text. When the scan
    reaches such a region with NO active code state, the whole region is
    copied verbatim and skipped -- backticks/fences inside a legitimate
    command body can no longer open a span that swallows the closing tag.
    Regions reached while already inside a fence or inline span keep the old
    behavior (masked through), which preserves the codex12 #2 immunity for
    convention echoes inside code fences / inline quotes.
    """
    chars = list(text)
    index = 0
    fence: str | None = None
    inline_ticks = 0
    shield_index = 0
    while index < len(text):
        if shield_spans:
            while (
                shield_index < len(shield_spans)
                and shield_spans[shield_index][1] <= index
            ):
                shield_index += 1
            if (
                shield_index < len(shield_spans)
                and shield_spans[shield_index][0] == index
                and fence is None
                and not inline_ticks
            ):
                index = shield_spans[shield_index][1]
                shield_index += 1
                continue
        if fence is not None:
            end = text.find(fence, index)
            if end < 0:
                for pos in range(index, len(chars)):
                    chars[pos] = " "
                break
            for pos in range(index, end + len(fence)):
                chars[pos] = " "
            index = end + len(fence)
            fence = None
            continue
        if inline_ticks:
            marker = "`" * inline_ticks
            end = text.find(marker, index)
            if end < 0:
                for pos in range(index, len(chars)):
                    chars[pos] = " "
                break
            for pos in range(index, end + inline_ticks):
                chars[pos] = " "
            index = end + inline_ticks
            inline_ticks = 0
            continue
        if text.startswith("```", index) or text.startswith("~~~", index):
            fence = text[index : index + 3]
            for pos in range(index, min(index + 3, len(chars))):
                chars[pos] = " "
            index += 3
            continue
        if text[index] == "`":
            run = 1
            while index + run < len(text) and text[index + run] == "`":
                run += 1
            inline_ticks = run
            for pos in range(index, min(index + run, len(chars))):
                chars[pos] = " "
            index += run
            continue
        index += 1
    return "".join(chars)


def _escape_control_chars_inside_json_strings(raw: str) -> str:
    """Repair common model JSON mistakes: raw control chars inside strings."""
    out: list[str] = []
    in_string = False
    escaped = False
    for char in raw:
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\" and in_string:
            out.append(char)
            escaped = True
            continue
        if char == '"':
            out.append(char)
            in_string = not in_string
            continue
        if in_string and ord(char) < 0x20:
            out.append(json.dumps(char)[1:-1])
        else:
            out.append(char)
    return "".join(out)


def _clean_parameter_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in {"$schema", "exclusiveMinimum", "exclusiveMaximum"}:
            continue
        if key == "description" and isinstance(value, str):
            first_line = value.strip().split("\n", 1)[0].strip()
            cleaned[key] = first_line[:120] if len(first_line) > 120 else first_line
        elif isinstance(value, dict):
            cleaned[key] = _clean_parameter_schema(value)
        elif isinstance(value, list):
            cleaned[key] = [
                _clean_parameter_schema(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_schema(value: Any, schema: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if isinstance(expected, str) and not _schema_type_matches(value, expected):
        raise MalformedToolCall(f"Tool argument {path} must be of type {expected}.")
    if (
        isinstance(expected, list)
        and expected
        and not any(
            isinstance(item, str) and _schema_type_matches(value, item)
            for item in expected
        )
    ):
        raise MalformedToolCall(f"Tool argument {path} does not match any allowed type.")
    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        raise MalformedToolCall(f"Tool argument {path} is not in the allowed enum.")
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [key for key in required if isinstance(key, str) and key not in value]
            if missing:
                raise MalformedToolCall(
                    f"Tool arguments are missing required field(s): {', '.join(missing)}"
                )
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, item in value.items():
                child_schema = properties.get(key)
                if child_schema is not None:
                    _validate_schema(item, child_schema, f"{path}.{key}")
                elif schema.get("additionalProperties") is False:
                    raise MalformedToolCall(f"Unexpected tool argument field: {key}")
        additional_schema = schema.get("additionalProperties")
        if isinstance(additional_schema, dict) and isinstance(properties, dict):
            for key, item in value.items():
                if key not in properties:
                    _validate_schema(item, additional_schema, f"{path}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_schema(item, schema["items"], f"{path}[{index}]")


def _strip_cdata(value: str) -> str:
    value = value.strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        return value[len("<![CDATA[") : -len("]]>")]
    if "<![CDATA[" not in value:
        return value
    parts: list[str] = []
    index = 0
    while True:
        start = value.find("<![CDATA[", index)
        if start < 0:
            tail = value[index:]
            if tail:
                parts.append(tail)
            break
        if start > index:
            parts.append(value[index:start])
        end = value.find("]]>", start)
        if end < 0:
            parts.append(value[start:])
            break
        parts.append(value[start + len("<![CDATA[") : end])
        index = end + len("]]>")
    return "".join(parts)


def _xml_entities_unescape(value: str) -> str:
    return (
        value.replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


def _coerce_xml_value(raw: str) -> Any:
    value = _xml_entities_unescape(_strip_cdata(raw))
    stripped = value.strip()
    lower = stripped.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower == "null":
        return None
    if re.fullmatch(r"[-+]?\d+", stripped):
        try:
            return int(stripped)
        except ValueError:
            return value
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", stripped):
        try:
            return float(stripped)
        except ValueError:
            return value
    if stripped.startswith(("[", "{")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


# Soft (stealth) protocol variant: the model was handed a conversational
# convention instead of an injected protocol block, so it emits plain-text
# <cmd>...</cmd> tags (one per shell action) or <json>...</json> / bare JSON
# function-call payloads.  Evidence: soft-framing probe 2026-08-24 -- the web
# model emitted exact <cmd> tags 2/2 under low-key framing while every request
# carrying an injected controller protocol block was refused 4/4.
_SOFT_CMD_TAG = "<cmd>"
_SOFT_CMD_CLOSE_TAG = "</cmd>"
_CMD_TAG_RE = re.compile(r"<cmd>\s*([\s\S]*?)\s*</cmd>")
_JSON_TAG_RE = re.compile(r"<json>\s*([\s\S]*?)\s*</json>")
# Codex13 finding #3 (2026-08-26): paired tag regions found on the RAW text so
# the markdown masker can treat legitimate unfenced <cmd>/<json> bodies as
# opaque spans instead of letting a literal unmatched backtick inside the body
# open an inline-code span that swallows the closing tag (which used to raise
# "Soft <cmd> tool call tags are incomplete." for valid shell like printf "`").
_SOFT_TAG_REGION_RE = re.compile(r"<cmd>[\s\S]*?</cmd>|<json>[\s\S]*?</json>")


def _soft_tag_regions(text: str) -> list[tuple[int, int]]:
    """Left-to-right non-overlapping paired soft-tag regions on raw ``text``."""
    return [(match.start(), match.end()) for match in _SOFT_TAG_REGION_RE.finditer(text)]

# debug-r9 BUG B (2026-08-25): the web model sometimes QUOTES the handshake
# format ("<cmd>...</cmd>") in prose while acknowledging the convention. The
# tag body is then a placeholder, not a shell command -- shipping it verbatim
# produced a live Bash(command="...") tool_use whose execution wasted a round
# on exit=127 "…: command not found" (session wgs_cc322123c8f5415b). Such
# bodies are treated as quoted protocol text: the tag span is excised from the
# reply and NO call is emitted (raising would drag an innocent acknowledgment
# into a pointless MALFORMED_TOOL correction loop).
_CMD_PLACEHOLDER_QUOTE_CHARS = "\"'`“”«»\u2018\u2019"
_CMD_PLACEHOLDER_ELLIPSIS_RE = re.compile(r"[.…⋯]+")
_CMD_PLACEHOLDER_PHRASES = frozenset(
    {
        "command",
        "<command>",
        "{command}",
        "your command",
        "the exact shell command",
    }
)


def _is_placeholder_command(command: str) -> bool:
    """True when a soft <cmd> body is a stand-in, not an executable command.

    Matches empty-ish bodies made only of quotes/dots/ellipsis ("...", '…',
    <<<...>>>) and the handshake's own literal placeholder phrases.
    """
    unquoted = command.strip().strip(_CMD_PLACEHOLDER_QUOTE_CHARS).strip()
    if not unquoted or _CMD_PLACEHOLDER_ELLIPSIS_RE.fullmatch(unquoted):
        return True
    normalized = re.sub(r"\s+", " ", unquoted).casefold().strip(" .…")
    return normalized in _CMD_PLACEHOLDER_PHRASES


def _extract_soft_candidates(
    text: str,
    masked: str,
    definitions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[int, int]]] | None:
    """Collect soft-protocol tool-call attempts from ``text``.

    Codex12 finding #2 (2026-08-26): tag detection runs on ``masked`` (the
    markdown-masked text from _mask_markdown_code), so a model merely ECHOING
    the convention inside a fenced ``` block or an inline-quoted `` `...` ``
    span never produces an executable call -- the same untrusted-echo rule the
    XML/DSML/legacy protocols already follow.  The handshake teaches plain
    unfenced emission, so legitimate <cmd>/<json> output is unaffected.
    ``masked`` preserves character offsets, so tag bodies are extracted from
    the ORIGINAL ``text`` at the match offsets (mirroring
    _parse_markup_blocks): commands containing balanced backticks keep
    parsing verbatim.  The bare/fenced JSON fallback below still scans the
    raw text because ```json fences ARE its canonical emission shape.

    Priority: ``<cmd>`` tags first (each becomes one canonical shell call),
    then ``<json>`` tags (OpenAI function-calling payloads), then bare/fenced
    JSON via the json-fn path.  Returns ``(raw_calls, spans)`` or ``None``
    when there is no attempt at all (plain prose).  A present-but-broken
    attempt raises MalformedToolCall (fail-closed), mirroring the other
    protocols.
    """
    raw_calls: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    cmd_opens = masked.count(_SOFT_CMD_TAG)
    cmd_closes = masked.count(_SOFT_CMD_CLOSE_TAG)
    if (cmd_opens or cmd_closes) and ("<json>" in masked or "</json>" in masked):
        # T2 root fix 2026-08-25: mixing emit shapes stays fail-closed even
        # though soft prose tolerance allows natural-language chatter.
        raise MalformedToolCall(
            "Soft reply mixes <cmd> and <json> tool-call shapes; "
            "emit exactly one shape."
        )
    if cmd_opens or cmd_closes:
        if cmd_opens != cmd_closes:
            raise MalformedToolCall("Soft <cmd> tool call tags are incomplete.")
        matches = list(_CMD_TAG_RE.finditer(masked))
        if len(matches) != cmd_opens:
            raise MalformedToolCall("Soft <cmd> tool call tags are malformed.")
        shell_name = _shell_tool_name(definitions)
        if shell_name is None:
            # T2 root fix 2026-08-25: never fabricate a virtual "Bash" tool
            # that canonicalization then rejects with the misleading
            # "Unknown tool requested: Bash". A shell command cannot be
            # safely re-targeted at an arbitrary non-shell tool either, so
            # fail closed here with an actionable configuration error that
            # names the declared surface.
            available = ", ".join(sorted(definitions)) or "<no tools declared>"
            raise MalformedToolCall(
                "Soft <cmd> tool call requires a shell tool named Bash/bash "
                f"on the client tool surface; declared tools: {available}."
            )
        for match in matches:
            # Body comes from the ORIGINAL text between the tag literals at
            # preserved offsets (group(1) bounds are computed on the masked
            # string, where blanked body characters collapse into the \s*
            # padding and would truncate the command).
            command = (
                text[match.start() + len(_SOFT_CMD_TAG) : match.end() - len(_SOFT_CMD_CLOSE_TAG)]
            ).strip()
            if not command:
                raise MalformedToolCall("Soft <cmd> tag contains an empty command.")
            if _is_placeholder_command(command):
                # debug-r9 BUG B: a quoted/ellipsised body is the model quoting
                # the protocol format, not requesting execution.  Keep the span
                # (so the quote is excised from the visible prose) but emit no
                # tool call for it.
                spans.append(match.span())
                continue
            raw_calls.append({"name": shell_name, "arguments": {"command": command}})
            spans.append(match.span())
        return raw_calls, spans
    json_opens = masked.count("<json>")
    json_closes = masked.count("</json>")
    if json_opens or json_closes:
        if json_opens != json_closes:
            raise MalformedToolCall("Soft <json> tool call tags are incomplete.")
        matches = list(_JSON_TAG_RE.finditer(masked))
        if len(matches) != json_opens:
            raise MalformedToolCall("Soft <json> tool call tags are malformed.")
        for index, match in enumerate(matches):
            payload = _loads_json_tool_payload(
                text[match.start() + len("<json>") : match.end() - len("</json>")].strip(),
                context=f"Soft <json> tool call tag #{index}",
            )
            raw_calls.extend(_normalize_json_fn_entries(payload))
            spans.append(match.span())
        return raw_calls, spans
    # No explicit soft tag: fall back to the json-fn shapes (bare JSON array,
    # ```json fence, stripped-fence "JSON\n..." artifact).
    extraction = _extract_json_fn_candidates(text)
    if extraction is None:
        return None
    for payload in extraction[0]:
        raw_calls.extend(_normalize_json_fn_entries(payload))
    spans.extend(extraction[1])
    return raw_calls, spans


def resolve_tool_protocol(value: str | None = None) -> str:
    """Resolve the controller tool protocol.

    ``xml`` (default), ``json-fn``, ``both`` teach an explicit tool-call emit
    format via build_tool_instructions().  ``soft`` is the stealth protocol:
    no protocol block is ever injected into the prompt; the model is handed
    the ``<cmd>...``/``<json>...`` convention by a one-time conversational
    handshake instead, and parse_tool_calls() accepts those tags (plus the
    json-fn shapes) on every response.  Reads ``WEBGPT_TOOL_PROTOCOL`` when
    ``value`` is not given explicitly.  Unknown values raise ValueError so
    misconfiguration fails loudly instead of silently degrading tool-call
    detection.
    """
    raw = value if value is not None else os.environ.get(_TOOL_PROTOCOL_ENV)
    normalized = (raw or "xml").strip().lower()
    if normalized not in _TOOL_PROTOCOLS:
        raise ValueError(
            f"Unsupported {_TOOL_PROTOCOL_ENV} {raw!r}; "
            f"expected one of: {', '.join(_TOOL_PROTOCOLS)}."
        )
    return normalized


def _loads_json_tool_payload(raw: str, *, context: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            repaired = _escape_control_chars_inside_json_strings(raw)
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise MalformedToolCall(f"{context} contains invalid JSON.") from exc


def _normalize_json_fn_entries(payload: Any) -> list[dict[str, Any]]:
    """Accept one call object, or an array of call objects, and normalize entries.

    OpenAI function-calling style encodes ``arguments`` as a JSON string; both a
    JSON object and such a string are accepted.  Shape errors stay fail-closed.
    """
    if isinstance(payload, dict):
        entries = [payload]
    elif isinstance(payload, list):
        entries = payload
    else:
        raise MalformedToolCall(
            "JSON tool call payload must be an object or an array of objects."
        )
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MalformedToolCall(f"JSON tool call entry #{index} must be an object.")
        if (
            set(entry) == {"name", "arguments"}
            and isinstance(entry["arguments"], str)
        ):
            entry = {
                "name": entry["name"],
                "arguments": _loads_json_tool_payload(
                    entry["arguments"],
                    context=f"JSON tool call entry #{index} arguments",
                ),
            }
        normalized.append(entry)
    return normalized


def _extract_json_fn_candidates(
    text: str,
) -> tuple[list[Any], list[tuple[int, int]]] | None:
    """Collect tool-call-shaped JSON payloads from ``text``.

    Returns ``(payloads, spans)`` when the text carries at least one JSON
    tool-call attempt, or ``None`` when there is no attempt at all (plain
    prose).  A present-but-broken attempt raises MalformedToolCall
    (fail-closed), mirroring the XML/DSML/legacy behavior.
    """
    payloads: list[Any] = []
    spans: list[tuple[int, int]] = []
    openers = list(_JSON_FENCE_OPEN_RE.finditer(text))
    if openers:
        matches = list(_JSON_FENCE_RE.finditer(text))
        if len(matches) != len(openers):
            raise MalformedToolCall("JSON tool call fence is unterminated.")
        for match in matches:
            stripped = match.group(1).strip()
            if not stripped.startswith(("[", "{")):
                # A ```json fence without an object/array payload is ordinary
                # Markdown content, not a tool-call attempt.
                continue
            payloads.append(
                _loads_json_tool_payload(stripped, context="JSON tool call fence")
            )
            spans.append((match.start(), match.end()))
    if not payloads:
        stripped_text = text.strip()
        candidate: tuple[str, str] | None = None
        if stripped_text.startswith(("[", "{")):
            candidate = ("Bare JSON tool call payload", stripped_text)
        else:
            # Live evidence: ```json fences rendered through ChatGPT Web DOM
            # scrape lose their backticks, surviving as the literal artifact
            # ``JSON\n[...]`` / ``JSON\n{...}``.  Accept that label prefix.
            label_match = re.match(r"JSON\b[\s]*[\r\n]+([\s\S]*)$", stripped_text)
            if label_match and label_match.group(1).strip().startswith(("[", "{")):
                candidate = (
                    'Stripped-fence JSON tool call payload ("JSON\\n..." artifact)',
                    label_match.group(1).strip(),
                )
        if candidate is not None:
            context, body = candidate
            payloads.append(_loads_json_tool_payload(body, context=context))
            spans.append((0, len(text)))
    if not payloads:
        return None
    return payloads, spans


def _parse_markup_blocks(
    original: str,
    masked: str,
    block_re: re.Pattern[str],
    invoke_re: re.Pattern[str],
    param_re: re.Pattern[str],
    *,
    open_tag: str,
    close_tag: str,
) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
    blocks = list(block_re.finditer(masked))
    if not blocks:
        return [], []
    if masked.count(open_tag) != masked.count(close_tag) or masked.count(open_tag) != len(blocks):
        raise MalformedToolCall("Tool XML/DSML blocks are incomplete or nested.")
    calls: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    for block in blocks:
        body = original[block.start(1) : block.end(1)]
        invokes = list(invoke_re.finditer(body))
        if not invokes:
            raise MalformedToolCall("Tool XML/DSML block contains no invoke entries.")
        consumed = list(body)
        for invoke in invokes:
            for pos in range(invoke.start(), invoke.end()):
                consumed[pos] = " "
            name = _xml_entities_unescape(invoke.group(1).strip())
            params_body = invoke.group(2)
            params = list(param_re.finditer(params_body))
            if not params:
                raise MalformedToolCall("Tool invoke contains no parameters.")
            param_consumed = list(params_body)
            arguments: dict[str, Any] = {}
            for param in params:
                for pos in range(param.start(), param.end()):
                    param_consumed[pos] = " "
                key = _xml_entities_unescape(param.group(1).strip())
                if key in arguments:
                    raise MalformedToolCall(f"Duplicate tool argument field: {key}")
                arguments[key] = _coerce_xml_value(param.group(2))
            if "".join(param_consumed).strip():
                raise MalformedToolCall("Tool invoke contains unsupported XML outside parameters.")
            calls.append({"name": name, "arguments": arguments})
        if "".join(consumed).strip():
            raise MalformedToolCall("Tool XML/DSML block contains unsupported text outside invokes.")
        spans.append((block.start(), block.end()))
    return calls, spans




def _repair_collapsed_python_body(body: str) -> str:
    # Common model mistake after XML/CDATA: Python source collapsed into a
    # single line.  Repair the smallest safe subset used in shell heredocs.
    body = re.sub(r"(def\s+\w+\([^)]*\):)\s+(return\b)", r"\1\n    \2", body)
    body = re.sub(r"(if\s+__name__\s*==\s*['\"]__main__['\"]\s*:)\s+(raise\b)", r"\1\n    \2", body)
    return body


def _parent_mkdir_prelude(command: str) -> str:
    parents: list[str] = []
    for match in re.finditer(r"cat\s+>\s+([^\s<>|;&]+)", command):
        raw_path = match.group(1).strip().strip("'\"")
        if "/" not in raw_path:
            continue
        parent = raw_path.rsplit("/", 1)[0]
        if parent and parent not in parents:
            parents.append(parent)
    if not parents:
        return ""
    quoted = " ".join("'" + item.replace("'", "'\''") + "'" for item in parents)
    return f"mkdir -p {quoted}"


def _repair_bash_command(command: str) -> str:
    mkdir_prelude = _parent_mkdir_prelude(command)
    if mkdir_prelude and mkdir_prelude not in command:
        command = mkdir_prelude + "\n" + command
    if "<<'" not in command:
        return command
    pattern = re.compile(
        r"(?P<head>cat\s+>\s+[^\n]+?\s+<<'(?P<tag>[A-Za-z_][A-Za-z0-9_]*)')\s+"
        r"(?P<body>.*?)\s+(?P=tag)(?P<tail>(?:\s+|$).*)",
        re.DOTALL,
    )
    previous = None
    repaired = command
    while previous != repaired:
        previous = repaired
        match = pattern.search(repaired)
        if not match:
            break
        body = _repair_collapsed_python_body(match.group("body"))
        tail = match.group("tail")
        if tail.strip():
            tail = "\n" + tail.strip()
        repaired = (
            repaired[: match.start()]
            + match.group("head")
            + "\n"
            + body
            + "\n"
            + match.group("tag")
            + tail
            + repaired[match.end() :]
        )
    return repaired

class ToolTranspiler:
    """Strict controller/model tool protocol mapped to canonical tool calls."""

    @staticmethod
    def effective_model_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Tools shown to ChatGPT Web, including safe gateway virtual tools.

        Claude Code 2.x exposes Bash/Edit/Read but may omit Write.  ChatGPT Web
        is much more reliable at preserving source indentation when it can emit
        a structured Write(file_path, content) call.  The gateway translates that
        virtual Write into an actual Bash tool call before returning to Claude
        Code, so the external client only executes tools it really provided.
        """
        effective = list(tools)
        if _shell_tool_name(tools) is not None and not _has_tool(tools, "Write"):
            effective.append(_VIRTUAL_WRITE_TOOL)
        return effective

    @staticmethod
    def validate_tools(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        validated: dict[str, dict[str, Any]] = {}
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise ValueError("Only tools with type='function' are supported.")
            function = tool.get("function")
            if not isinstance(function, dict):
                raise ValueError("Tool function must be an object.")
            name = function.get("name")
            if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
                raise ValueError(f"Invalid tool name: {name!r}")
            if name in validated:
                raise ValueError(f"Duplicate tool name: {name}")
            parameters = function.get("parameters", {"type": "object"})
            if not isinstance(parameters, dict):
                raise ValueError(f"parameters for {name} must be an object.")
            validated[name] = function
        return validated

    @staticmethod
    def validate_tool_choice(tools: list[dict[str, Any]], tool_choice: Any = "auto") -> None:
        available = ToolTranspiler.validate_tools(tools)
        if tool_choice is None:
            return
        if isinstance(tool_choice, str):
            if tool_choice not in {"auto", "none", "required"}:
                raise ValueError(f"Unsupported tool_choice: {tool_choice!r}")
            if tool_choice == "required" and not available:
                raise ValueError("tool_choice='required' requires at least one tool.")
            return
        if not isinstance(tool_choice, dict):
            raise ValueError(f"Unsupported tool_choice: {tool_choice!r}")
        if tool_choice.get("type") != "function":
            raise ValueError("Only function tool_choice objects are supported.")
        function = tool_choice.get("function")
        selected = function.get("name") if isinstance(function, dict) else None
        if selected not in available:
            raise ValueError(f"tool_choice refers to an unknown tool: {selected}")

    @classmethod
    def build_tool_instructions(
        cls,
        tools: list[dict[str, Any]],
        tool_choice: Any = "auto",
        protocol: str | None = None,
    ) -> str:
        resolved_protocol = resolve_tool_protocol(protocol)
        model_tools = cls.effective_model_tools(tools)
        available = cls.validate_tools(model_tools)
        cls.validate_tool_choice(model_tools, tool_choice)
        if not available:
            return ""
        if resolved_protocol == "soft":
            # Stealth protocol: the emit convention is negotiated by a
            # conversational handshake in the first user turn, never by an
            # injected instructions block (soft-framing probe 2026-08-24).
            return ""
        choice_instruction = "Use a tool only when needed."
        if tool_choice == "none":
            choice_instruction = "Do not call any tool; answer using available information."
        elif tool_choice == "required":
            choice_instruction = "You must call at least one available tool."
        elif isinstance(tool_choice, dict):
            function = tool_choice.get("function")
            selected = function.get("name") if isinstance(function, dict) else None
            if selected not in available:
                raise ValueError(f"tool_choice refers to an unknown tool: {selected}")
            choice_instruction = f"You must call exactly the tool named {selected}."
        declarations = [
            {
                "name": name,
                "description": (definition.get("description", "") or "")
                .strip()
                .split("\n", 1)[0][:120],
                "parameters": _clean_parameter_schema(
                    definition.get("parameters", {"type": "object"})
                ),
            }
            for name, definition in available.items()
        ]
        shell_name = _shell_tool_name(available) or "shell"
        if resolved_protocol == "json-fn":
            return (
                "WEBGPT CONTROLLER TOOL PROTOCOL (highest priority for tool formatting):\n"
                f"Available tools: {json.dumps(declarations, ensure_ascii=False, separators=(',', ':'))}\n"
                f"{choice_instruction}\n"
                + cls._json_fn_instructions(shell_name)
            )
        shell_description_parameter = (
            "    <parameter name=\"description\"><![CDATA[Print working directory]]></parameter>\n"
            if shell_name == "Bash"
            else ""
        )
        heredoc_description_parameter = (
            "    <parameter name=\"description\"><![CDATA[Create and compile a Python file with preserved indentation]]></parameter>\n"
            if shell_name == "Bash"
            else ""
        )
        return (
            "WEBGPT CONTROLLER TOOL PROTOCOL (highest priority for tool formatting):\n"
            f"Available tools: {json.dumps(declarations, ensure_ascii=False, separators=(',', ':'))}\n"
            f"{choice_instruction}\n"
            "You are connected to an external execution environment with the above tools. "
            "Whenever you need to inspect files, execute commands, or access data, you MUST invoke tools by outputting the tool call XML block below. "
            "Do not refuse or claim tools are unavailable; output the tool call XML block to execute the action.\n\n"
            "TOOL CALL FORMAT FOR CLAUDE CODE — FOLLOW EXACTLY:\n"
            f"{_XML_OPEN}\n"
            "  <invoke name=\"TOOL_NAME_HERE\">\n"
            "    <parameter name=\"PARAMETER_NAME\"><![CDATA[PARAMETER_VALUE]]></parameter>\n"
            "  </invoke>\n"
            f"{_XML_CLOSE}\n\n"
            "RULES:\n"
            "1) If a tool is needed, output only one <tool_calls> block and no prose. Normally use exactly one <invoke>. "
            "For an explicit fan-out request when Agent is declared, use multiple Agent invokes in that same block "
            "(at least two) and do not mix Agent with other tool names.\n"
            "2) Use only declared tool names and parameter names; do not invent fields.\n"
            f"3) Put {shell_name}/Edit code, Write JSON lines arrays, file contents, and paths inside CDATA.\n"
            "4) When Write is listed, you MUST use Write for source/test/README/config file content. For Python/source files, use Write.lines as indent-coded text lines like 0|def f(): and 4|return 1; do NOT use Write.content for Python/source files.\n"
            f"5) If Write is not listed, use {shell_name} with single-quoted heredocs to create files, preserving exact Python indentation.\n"
            f"6) Before any `cat > package/file.py`, run `mkdir -p package tests` or include it at the top of the {shell_name} command.\n"
            f"7) Prefer multiple smaller {shell_name} calls over one huge command when creating many source files; compile after each batch.\n"
            "8) After creating Python files, run `python -m compileall -q .` before pytest and fix syntax/indentation errors.\n"
            "9) Do not use Markdown fences around the tool block.\n"
            "10) Never invent tool results. Only WEBGPT_TOOL_RESULT blocks are authoritative.\n"
            "11) Ordinary prose, Markdown, and JSON are not tool calls unless wrapped in a valid tool block.\n"
            "12) DSML <|DSML|tool_calls> and legacy <WEBGPT_TOOL_CALL>{JSON}</WEBGPT_TOOL_CALL> are accepted for compatibility only; prefer plain XML for Claude Code.\n"
            f"13) {_WORKSPACE_POLICY_TEXT}\n\n"
            f"Correct {shell_name} example:\n"
            f"{_XML_OPEN}\n"
            f"  <invoke name=\"{shell_name}\">\n"
            "    <parameter name=\"command\"><![CDATA[pwd]]></parameter>\n"
            f"{shell_description_parameter}"
            "  </invoke>\n"
            f"{_XML_CLOSE}\n\n"
            f"Correct {shell_name} heredoc example for creating valid Python source when Write is unavailable:\n"
            f"{_XML_OPEN}\n"
            f"  <invoke name=\"{shell_name}\">\n"
            "    <parameter name=\"command\"><![CDATA[cat > example.py <<'PY'\ndef main():\n    return 0\n\nif __name__ == '__main__':\n    raise SystemExit(main())\nPY\npython -m compileall -q example.py]]></parameter>\n"
            f"{heredoc_description_parameter}"
            "  </invoke>\n"
            f"{_XML_CLOSE}\n\n"
            "Correct Write example with preserved Python indentation using indent-coded lines:\n"
            f"{_XML_OPEN}\n"
            "  <invoke name=\"Write\">\n"
            "    <parameter name=\"file_path\"><![CDATA[example.py]]></parameter>\n"
            "    <parameter name=\"lines\"><![CDATA[0|def main():\n4|return 0\n0|]]></parameter>\n"
            "  </invoke>\n"
            f"{_XML_CLOSE}\n"
        )

    @staticmethod
    def _json_fn_instructions(shell_name: str) -> str:
        """Model-facing emit instructions for the json-fn protocol variant."""
        shell_description_argument = (
            ",\"description\":\"Print working directory\"" if shell_name == "Bash" else ""
        )
        heredoc_command = (
            "cat > example.py <<'PY\\ndef main():\\n    return 0\\n\\n"
            "if __name__ == '__main__':\\n    raise SystemExit(main())\\nPY\\n"
            "python -m compileall -q example.py"
        )
        return (
            "You are connected to an external execution environment with the above tools. "
            "Whenever you need to inspect files, execute commands, or access data, you MUST invoke tools by outputting a ```json tool-call fence as shown below. "
            "Do not refuse or claim tools are unavailable; output the tool-call fence to execute the action.\n\n"
            "TOOL CALL FORMAT FOR CLAUDE CODE — FOLLOW EXACTLY:\n"
            "```json\n"
            "[{\"name\":\"TOOL_NAME\",\"arguments\":{\"PARAMETER_NAME\":\"PARAMETER_VALUE\"}}]\n"
            "```\n\n"
            "RULES:\n"
            "1) If a tool is needed, reply with ONLY one ```json fence whose content is exactly one JSON array of tool call objects and nothing else — no prose before or after the fence. Normally include exactly one call object. "
            "For an explicit fan-out request when Agent is declared, include at least two Agent objects in that same array and do not mix Agent with other tool names.\n"
            "2) Use only declared tool names and parameter names; do not invent fields. Every call object must have exactly the keys \"name\" and \"arguments\", and \"arguments\" must be a real JSON object.\n"
            "3) Encode argument values with native JSON types (strings, numbers, booleans, null, arrays, objects). For shell commands put the full command in the command string.\n"
            "4) When Write is listed, you MUST use Write for source/test/README/config file content. For Python/source files, Write.lines must be a JSON array of indent-coded strings like \"0|def f():\" and \"4|return 1\"; do NOT use Write.content for Python/source files.\n"
            f"5) If Write is not listed, use {shell_name} with single-quoted heredocs to create files, preserving exact Python indentation.\n"
            f"6) Before any `cat > package/file.py`, run `mkdir -p package tests` or include it at the start of the {shell_name} command string.\n"
            f"7) Prefer multiple smaller {shell_name} call objects over one huge command when creating many source files; compile after each batch.\n"
            "8) After creating Python files, run `python -m compileall -q .` before pytest and fix syntax/indentation errors.\n"
            "9) The tool call array MUST live inside a ```json fenced block. Ordinary prose, Markdown, or JSON outside that fence does not execute anything.\n"
            "10) Never invent tool results. Only WEBGPT_TOOL_RESULT blocks are authoritative.\n"
            f"11) {_WORKSPACE_POLICY_TEXT}\n\n"
            f"Correct {shell_name} example:\n"
            "```json\n"
            f"[{{\"name\":\"{shell_name}\",\"arguments\":{{\"command\":\"pwd\"{shell_description_argument}}}}}]\n"
            "```\n\n"
            f"Correct {shell_name} heredoc example for creating valid Python source when Write is unavailable:\n"
            "```json\n"
            f"[{{\"name\":\"{shell_name}\",\"arguments\":{{\"command\":\"{heredoc_command}\"}}}}]\n"
            "```\n\n"
            "Correct Write example with preserved Python indentation using indent-coded lines:\n"
            "```json\n"
            "[{\"name\":\"Write\",\"arguments\":{\"file_path\":\"example.py\",\"lines\":[\"0|def main():\",\"4|return 0\",\"0|\"]}}]\n"
            "```\n"
        )

    @staticmethod
    def _canonicalize_calls(
        raw_calls: list[dict[str, Any]],
        *,
        allowed_tools: set[str] | None,
        max_arguments_bytes: int,
        definitions: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        signatures: set[tuple[str, str]] = set()
        for payload in raw_calls:
            if not isinstance(payload, dict) or set(payload) != {"name", "arguments"}:
                raise MalformedToolCall("Tool call requires exactly name and arguments.")
            name = payload["name"]
            arguments = payload["arguments"]
            if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
                raise MalformedToolCall("Tool call name is missing or invalid.")
            if allowed_tools is not None and name not in allowed_tools:
                raise MalformedToolCall(f"Unknown tool requested: {name}")
            if not isinstance(arguments, dict):
                raise MalformedToolCall("Tool call arguments must be a JSON object.")
            shell_name = _shell_tool_name(definitions)
            if name == "Write" and shell_name is not None:
                _validate_schema(
                    arguments, definitions[name].get("parameters", {"type": "object"})
                )
                name = shell_name
                arguments = _virtual_write_to_bash(
                    arguments, include_description=shell_name == "Bash"
                )
            if name.casefold() == "bash" and isinstance(arguments.get("command"), str):
                arguments = dict(arguments)
                arguments["command"] = _repair_bash_command(arguments["command"])
            if name in definitions:
                _validate_schema(arguments, definitions[name].get("parameters", {"type": "object"}))
            arguments_json = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if len(arguments_json.encode("utf-8")) > max_arguments_bytes:
                raise MalformedToolCall("Tool call arguments exceed the configured limit.")
            signature = (name, arguments_json)
            if signature in signatures:
                raise MalformedToolCall("Duplicate tool call blocks are ambiguous.")
            signatures.add(signature)
            calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments_json},
                }
            )
        return calls

    @classmethod
    def _finalize_parse(
        cls,
        text: str,
        *,
        raw_calls: list[dict[str, Any]],
        spans: list[tuple[int, int]],
        markup_present: bool,
        allowed_tools: set[str] | None,
        max_arguments_bytes: int,
        definitions: dict[str, dict[str, Any]],
        allow_prose: bool = False,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        if not raw_calls:
            if markup_present:
                raise MalformedToolCall("Tool block did not contain any valid tool calls.")
            if spans:
                # Codex12 finding #5 (2026-08-26): a placeholder-only soft
                # reply records its tag spans but emits no call.  The
                # debug-r9 contract promises the quoted protocol fragment is
                # excised from the visible prose -- honor that even when
                # every tag was filtered out.
                outside_chars = list(text)
                for start, end in spans:
                    for pos in range(start, end):
                        outside_chars[pos] = " "
                return "".join(outside_chars).strip(), []
            return text, []
        outside_chars = list(text)
        for start, end in spans:
            for pos in range(start, end):
                outside_chars[pos] = " "
        outside_prose = "".join(outside_chars).strip()
        if outside_prose:
            if not allow_prose:
                raise MalformedToolCall("Tool call cannot be mixed with final assistant prose.")
            prose_clean = outside_prose
        else:
            prose_clean = None
        return prose_clean, cls._canonicalize_calls(
            raw_calls,
            allowed_tools=allowed_tools,
            max_arguments_bytes=max_arguments_bytes,
            definitions=definitions,
        )

    @classmethod
    def parse_tool_calls(
        cls,
        text: str,
        allowed_tools: set[str] | None = None,
        max_arguments_bytes: int = 65_536,
        tool_definitions: list[dict[str, Any]] | None = None,
        allow_prose: bool = False,
        protocol: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        if not isinstance(text, str) or not text:
            return None, []
        resolved_protocol = resolve_tool_protocol(protocol)
        masked = _mask_markdown_code(text)
        has_legacy = _OPEN in masked or _CLOSE in masked
        has_dsml = _DSML_OPEN in masked or _DSML_CLOSE in masked
        has_xml = _XML_OPEN in masked or _XML_CLOSE in masked
        model_tool_definitions = cls.effective_model_tools(tool_definitions or [])
        definitions = cls.validate_tools(model_tool_definitions)
        if not (has_legacy or has_dsml or has_xml):
            if resolved_protocol == "soft":
                # Codex12 finding #2: scan the markdown-masked text so
                # <cmd>/<json> tags echoed inside code fences or inline
                # quotes are never executed; bodies still come from the
                # original offsets inside _extract_soft_candidates().
                # Codex13 finding #3: the soft scan additionally shields
                # paired raw-text tag regions so a literal unmatched backtick
                # INSIDE a legitimate body cannot swallow its closing tag.
                soft = _extract_soft_candidates(
                    text,
                    _mask_markdown_code(text, shield_spans=_soft_tag_regions(text)),
                    definitions,
                )
                if soft is None:
                    return text, []
                soft_raw_calls = soft[0]
                soft_spans = soft[1]
                # SOFT PROSE TOLERANCE (T2 root fix 2026-08-25): the stealth
                # handshake teaches the <cmd>/<json> convention conversationally,
                # so web models routinely append natural-language chatter around
                # a perfectly valid tool tag ("Need next step..."). Rejecting
                # that as malformed discarded the executable call. Fail-closed
                # prose rejection stays for xml/json-fn protocols (and for
                # markup blocks, which soft never teaches).
                return cls._finalize_parse(
                    text,
                    raw_calls=soft_raw_calls,
                    spans=soft_spans,
                    markup_present=False,
                    allowed_tools=allowed_tools,
                    max_arguments_bytes=max_arguments_bytes,
                    definitions=definitions,
                    allow_prose=True,
                )
            if resolved_protocol not in {"json-fn", "both"}:
                return text, []
        if allowed_tools is not None:
            allowed_tools = set(allowed_tools)
            if any(name in allowed_tools for name in ("Bash", "bash")) and "Write" in definitions:
                allowed_tools.add("Write")
        raw_calls: list[dict[str, Any]] = []
        spans: list[tuple[int, int]] = []

        if has_dsml:
            parsed, parsed_spans = _parse_markup_blocks(
                text,
                masked,
                _DSML_BLOCK_RE,
                _DSML_INVOKE_RE,
                _DSML_PARAM_RE,
                open_tag=_DSML_OPEN,
                close_tag=_DSML_CLOSE,
            )
            raw_calls.extend(parsed)
            spans.extend(parsed_spans)
        if has_xml:
            parsed, parsed_spans = _parse_markup_blocks(
                text,
                masked,
                _XML_BLOCK_RE,
                _XML_INVOKE_RE,
                _XML_PARAM_RE,
                open_tag=_XML_OPEN,
                close_tag=_XML_CLOSE,
            )
            raw_calls.extend(parsed)
            spans.extend(parsed_spans)
        if has_legacy:
            blocks = list(_BLOCK_RE.finditer(masked))
            if masked.count(_OPEN) != masked.count(_CLOSE) or masked.count(_OPEN) != len(blocks) or not blocks:
                raise MalformedToolCall("WEBGPT_TOOL_CALL blocks are incomplete or nested.")
            for match in blocks:
                payload_text = text[match.start(1) : match.end(1)]
                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError:
                    try:
                        payload = json.loads(_escape_control_chars_inside_json_strings(payload_text))
                    except json.JSONDecodeError as exc:
                        raise MalformedToolCall("Tool call payload is invalid JSON.") from exc
                raw_calls.append(payload)
                spans.append((match.start(), match.end()))

        if not raw_calls and not (has_legacy or has_dsml or has_xml):
            # json-fn / both protocols: no markup block was present, so try the
            # OpenAI function-calling JSON shape (fenced or bare).  Markup
            # always wins when present ("both" accepts whichever matches).
            extraction = _extract_json_fn_candidates(text)
            if extraction is not None:
                for payload in extraction[0]:
                    raw_calls.extend(_normalize_json_fn_entries(payload))
                spans.extend(extraction[1])

        if not raw_calls:
            if has_legacy or has_dsml or has_xml:
                raise MalformedToolCall("Tool block did not contain any valid tool calls.")
            return text, []
        return cls._finalize_parse(
            text,
            raw_calls=raw_calls,
            spans=spans,
            markup_present=bool(has_legacy or has_dsml or has_xml),
            allowed_tools=allowed_tools,
            max_arguments_bytes=max_arguments_bytes,
            definitions=definitions,
            allow_prose=allow_prose,
        )


__all__ = ["ToolTranspiler", "resolve_tool_protocol"]
