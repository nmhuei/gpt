from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import Any

from gpt.state import MalformedResponse, ProtocolChanged


class SSEDecoder:
    """Incremental, UTF-8 safe SSE decoder used by observed protocol fixtures."""

    def __init__(self) -> None:
        self._buffer = ""
        self._utf8 = codecs.getincrementaldecoder("utf-8")()

    def feed(self, chunk: bytes | str) -> list[str]:
        if isinstance(chunk, bytes):
            try:
                chunk = self._utf8.decode(chunk, final=False)
            except UnicodeDecodeError as exc:
                raise MalformedResponse("Stream is not valid UTF-8.") from exc
        self._buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
        records: list[str] = []
        while "\n\n" in self._buffer:
            record, self._buffer = self._buffer.split("\n\n", 1)
            data = [line[5:].lstrip() for line in record.split("\n") if line.startswith("data:")]
            if data:
                records.append("\n".join(data))
        return records

    def finish(self) -> list[str]:
        try:
            self._buffer += self._utf8.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise MalformedResponse("Stream ended inside a UTF-8 sequence.") from exc
        if not self._buffer.strip():
            return []
        records = self.feed("\n\n")
        self._buffer = ""
        return records


def value_at(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


@dataclass(frozen=True)
class StreamContract:
    """Paths come from the evidence ledger, never from production guesses."""

    text_path: tuple[str, ...]
    status_path: tuple[str, ...]
    completion_values: frozenset[str]


class ObservedStreamParser:
    """Validates observed SSE fixtures while tolerating unknown optional events."""

    def __init__(self, contract: StreamContract):
        self.contract = contract
        self.decoder = SSEDecoder()
        self.text = ""
        self.completed = False

    def feed(self, chunk: bytes | str) -> list[str]:
        deltas: list[str] = []
        for record in self.decoder.feed(chunk):
            if record == "[DONE]":
                self.completed = True
                continue
            try:
                payload = json.loads(record)
            except json.JSONDecodeError as exc:
                raise MalformedResponse("SSE data field is not JSON.") from exc
            text = value_at(payload, self.contract.text_path)
            if isinstance(text, str):
                if text.startswith(self.text):
                    delta = text[len(self.text) :]
                    self.text = text
                else:
                    delta = text
                    self.text += text
                if delta:
                    deltas.append(delta)
            status = value_at(payload, self.contract.status_path)
            if isinstance(status, str) and status in self.contract.completion_values:
                self.completed = True
        return deltas

    def finish(self) -> str:
        for record in self.decoder.finish():
            self.feed(f"data: {record}\n\n")
        if not self.completed:
            raise ProtocolChanged("Observed stream ended without the required completion signal.")
        return self.text
