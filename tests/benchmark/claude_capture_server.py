from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _contains_tool_result(body: dict[str, Any]) -> bool:
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
            return True
    return False


def _write_capture(path: Path, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "method": "POST",
        "path": urlsplit(handler.path).path,
        "headers": {
            key: value
            for key, value in handler.headers.items()
            if key.casefold() in {"user-agent", "anthropic-version", "content-type", "accept"}
        },
        "body": body,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)


def _text_message() -> dict[str, Any]:
    return {
        "id": "msg_capture_final",
        "type": "message",
        "role": "assistant",
        "model": "claude-fable-5",
        "content": [{"type": "text", "text": "CLAUDE_CAPTURE_OK"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _tool_message() -> dict[str, Any]:
    return {
        "id": "msg_capture_tool",
        "type": "message",
        "role": "assistant",
        "model": "claude-fable-5",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_capture_bash",
                "name": "Bash",
                "input": {"command": "pwd", "description": "Capture a real Claude Code tool result"},
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _sse_events(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    block = message["content"][0]
    start_message = dict(message)
    start_message["content"] = []
    start_message["stop_reason"] = None
    events: list[tuple[str, dict[str, Any]]] = [
        ("message_start", {"type": "message_start", "message": start_message})
    ]
    if block["type"] == "tool_use":
        events.extend(
            [
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": block["id"],
                            "name": block["name"],
                            "input": {},
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(block["input"], separators=(",", ":")),
                        },
                    },
                ),
            ]
        )
    else:
        events.extend(
            [
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": block["text"]},
                    },
                ),
            ]
        )
    events.extend(
        [
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )
    return events


class CaptureServer(ThreadingHTTPServer):
    request_log: Path
    text_only: bool = False


class Handler(BaseHTTPRequestHandler):
    server: CaptureServer

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        if path == "/v1/models":
            payload = {
                "object": "list",
                "data": [{"id": "claude-fable-5", "object": "model", "created": 0, "owned_by": "capture"}],
            }
            self._json(payload)
            return
        self.send_error(404)

    def do_HEAD(self) -> None:
        if urlsplit(self.path).path == "/api/hello":
            self.send_response(200)
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/v1/messages":
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self.send_error(400)
            return
        if not isinstance(body, dict):
            self.send_error(400)
            return
        _write_capture(self.server.request_log, self, body)
        message = (
            _text_message()
            if self.server.text_only or _contains_tool_result(body)
            else _tool_message()
        )
        if body.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for event, payload in _sse_events(message):
                encoded = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                self.wfile.write(encoded)
                self.wfile.flush()
            return
        self._json(message)

    def _json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, message_format: str, *args: object) -> None:
        print(message_format % args, file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--request-log", type=Path, required=True)
    parser.add_argument("--text-only", action="store_true")
    args = parser.parse_args()
    server = CaptureServer(("127.0.0.1", args.port), Handler)
    server.request_log = args.request_log
    server.text_only = args.text_only
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
