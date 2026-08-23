from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        if urlsplit(self.path).path == "/v1/models":
            encoded = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "claude-fable-5",
                            "object": "model",
                            "created": 0,
                            "owned_by": "local-webgpt",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
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
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            request = json.loads(raw_body)
            print(
                json.dumps(
                    {
                        "model": request.get("model"),
                        "tool_names": [
                            tool.get("name")
                            for tool in request.get("tools", [])
                            if isinstance(tool, dict)
                        ],
                    }
                ),
                file=sys.stderr,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        payload = {
            "id": "msg_smoke",
            "type": "message",
            "role": "assistant",
            "model": "chatgpt-web",
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
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
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
