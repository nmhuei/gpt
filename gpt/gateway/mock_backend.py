from __future__ import annotations

import hashlib
import json
from typing import Any


def mock_argument_value(schema: Any, name: str) -> Any:
    if not isinstance(schema, dict):
        return "mock"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next(
            (item for item in kind if item != "null"),
            kind[0] if kind else None,
        )
    if kind == "boolean":
        return False
    if kind in {"integer", "number"}:
        return 1
    if kind == "array":
        return []
    if kind == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            return {}
        return {
            key: mock_argument_value(properties.get(key), key)
            for key in required
            if isinstance(key, str)
        }
    lowered = name.casefold()
    if lowered in {"file_path", "path", "filename"}:
        return "README.md"
    if lowered in {"command", "cmd"}:
        return "pwd"
    if lowered in {"description", "reason"}:
        return "Inspect the workspace"
    return "mock"


def mock_request_calls_for_tool(
    messages: list[dict[str, Any]],
    tools_by_name: dict[str, dict[str, Any]],
) -> bool:
    user_text = next(
        (
            str(message.get("content", "")).strip()
            for message in reversed(messages)
            if message.get("role") == "user"
            and str(message.get("content", "")).strip()
        ),
        "",
    ).casefold()
    if not user_text:
        return False
    if any(
        phrase in user_text
        for phrase in (
            "use a tool",
            "use the tool",
            "call a tool",
            "call the tool",
            "run a command",
            "execute a command",
            "read the file",
            "inspect the workspace",
        )
    ):
        return True
    return any(user_text.startswith(f"{name.casefold()} ") for name in tools_by_name)


def mock_tool_call(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: Any,
) -> list[dict[str, Any]] | None:
    if not tools or tool_choice == "none":
        return None
    names = {
        str(tool.get("function", {}).get("name")): tool
        for tool in tools
        if isinstance(tool.get("function"), dict)
        and isinstance(tool["function"].get("name"), str)
    }
    requested = None
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        if isinstance(function, dict):
            requested = function.get("name")
    if requested is None and not mock_request_calls_for_tool(messages, names):
        return None

    selected_name = requested if requested in names else next(
        (name for name in ("Read", "read", "Bash", "bash") if name in names),
        next(iter(names), None),
    )
    if selected_name is None:
        return None

    function = names[selected_name]["function"]
    parameters = function.get("parameters")
    properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    required = parameters.get("required", []) if isinstance(parameters, dict) else []
    arguments = {
        name: mock_argument_value(properties.get(name), name)
        for name in required
        if isinstance(name, str)
    }
    if selected_name.casefold() == "bash" and "command" in properties:
        arguments["command"] = "pwd"

    digest = hashlib.sha256(
        json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:16]
    return [
        {
            "id": f"call_mock_{digest}",
            "type": "function",
            "function": {
                "name": selected_name,
                "arguments": json.dumps(arguments),
            },
        }
    ]


def format_conversational_reply(user_text: str) -> str:
    cleaned = user_text.strip().casefold()
    if cleaned in {"hi", "hello", "hey", "chao", "chào", "xin chao", "xin chào"}:
        return "Xin chào! Tôi có thể giúp gì cho bạn hôm nay?"
    if any(q in cleaned for q in ["toio laf ai", "tôi là ai", "toi la ai", "who am i"]):
        return (
            "Bạn là lập trình viên / người dùng trên hệ thống, và tôi là trợ lý AI "
            "(Claude Code) đồng hành cùng bạn để phân tích, lập trình và giải quyết tác vụ."
        )
    if any(q in cleaned for q in ["ban la ai", "bạn là ai", "who are you"]):
        return (
            "Tôi là Claude Code - trợ lý AI lập trình được kết nối trực tiếp qua "
            "WebGPT Gateway."
        )
    if any(q in cleaned for q in ["giup gi", "giúp gì", "help", "can you help"]):
        return (
            "Tôi có thể hỗ trợ bạn đọc/viết mã nguồn, chạy lệnh shell, kiểm thử "
            "phần mềm, debug và xây dựng toàn bộ dự án."
        )
    return (
        f"Tôi đã tiếp nhận yêu cầu: {user_text.strip()}. "
        "Bạn cần tôi thực hiện bước nào tiếp theo?"
    )


__all__ = [
    "format_conversational_reply",
    "mock_argument_value",
    "mock_request_calls_for_tool",
    "mock_tool_call",
]
