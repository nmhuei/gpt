from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    request_log: str

    def _write_json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _write_sse(self, events: list[dict]) -> None:
        encoded = (
            "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            + "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _request_body(self) -> object:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return None
        raw_body = self.rfile.read(length)
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            return raw_body.decode(errors="replace")

    def _record(self, body: object | None = None) -> None:
        entry = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }
        with open(self.request_log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        self._record()
        if path == "/health":
            self._write_json(200, {"ok": True})
            return
        if path == "/v1/models":
            self._write_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "webgpt-opencode-fake",
                            "object": "model",
                            "created": 0,
                            "owned_by": "local-webgpt",
                        }
                    ],
                },
            )
            return
        self._write_json(404, {"error": {"message": f"not found: {path}"}})

    def do_HEAD(self) -> None:
        self._record()
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self._request_body()
        self._record(body)
        if path != "/v1/chat/completions":
            self._write_json(404, {"error": {"message": f"not found: {path}"}})
            return
        stream = isinstance(body, dict) and body.get("stream") is True
        if stream:
            self._write_sse(
                [
                    {
                        "id": "chatcmpl_opencode_smoke",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "webgpt-opencode-fake",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "OPENCODE_FAKE_OK"},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl_opencode_smoke",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "webgpt-opencode-fake",
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "stop"}
                        ],
                    },
                ]
            )
            return
        self._write_json(
            200,
            {
                "id": "chatcmpl_opencode_smoke",
                "object": "chat.completion",
                "created": 0,
                "model": "webgpt-opencode-fake",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OPENCODE_FAKE_OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    def log_message(self, message_format: str, *args: object) -> None:
        print(message_format % args, file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--request-log", required=True)
    args = parser.parse_args()
    Handler.request_log = args.request_log
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(server.server_port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
