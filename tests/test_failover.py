from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

from gpt.conversations import ConversationRecord, ConversationStore
from gpt.gateway.server import WebChatAPIServer
from gpt.state import (
    AuthRequired,
    BrowserDisconnected,
    CommitUnknown,
    EmptyModelResponse,
    RateLimited,
)
from gpt.transport.failover import FailoverRetryRequired, maybe_failover
from gpt.transport.multi_account import MultiAccountWorkerFactory


def _record(**overrides) -> ConversationRecord:
    record = ConversationRecord()
    record.account_name = "account-a"
    record.conversation_id = "conv-123"
    record.web_bootstrapped = True
    record.pending_request_fingerprint = "fp"
    record.pending_prompt = "prompt body"
    record.pending_submitted_at = 1.0
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def test_rate_limited_failover_resets_binding_and_pending():
    store = ConversationStore()
    record = _record()

    assert maybe_failover(record, RateLimited("429"), store=store) is True
    assert record.account_name is None
    assert record.conversation_id is None
    assert record.web_bootstrapped is False
    # Pending send cleared through the ConversationStore API.
    assert record.pending_request_fingerprint is None
    assert record.pending_prompt is None
    assert record.pending_submitted_at is None


def test_auth_required_only_before_bootstrap():
    fresh = _record(web_bootstrapped=False)
    assert maybe_failover(fresh, AuthRequired("login wall")) is True
    assert fresh.account_name is None
    assert fresh.conversation_id is None
    assert fresh.web_bootstrapped is False

    bootstrapped = _record(web_bootstrapped=True)
    assert maybe_failover(bootstrapped, AuthRequired("login wall")) is False
    # Fail-closed refusals must not mutate the record.
    assert bootstrapped.account_name == "account-a"
    assert bootstrapped.conversation_id == "conv-123"
    assert bootstrapped.web_bootstrapped is True


def test_commit_unknown_requires_reconciliation_verdict():
    # No reconciliation available -> fail closed.
    unknown = _record()
    exc = CommitUnknown("uncertain commit", conversation_id="conv-123", submitted=True)
    assert maybe_failover(unknown, exc) is False
    assert unknown.account_name == "account-a"

    # Reconcile proves the user turn landed -> never fail over.
    present = _record()
    assert (
        maybe_failover(present, exc, reconciled_user_turn_present=True) is False
    )
    assert present.account_name == "account-a"

    # Reconcile proves the user turn is absent -> safe reset.
    absent = _record()
    assert (
        maybe_failover(absent, exc, reconciled_user_turn_present=False) is True
    )
    assert absent.account_name is None
    assert absent.conversation_id is None
    assert absent.web_bootstrapped is False

    # Runtime asserted the send never happened -> safe without reconcile.
    not_submitted = _record()
    assert (
        maybe_failover(
            not_submitted,
            CommitUnknown("never sent", submitted=False),
        )
        is True
    )


def test_disabled_flag_never_fails_over(monkeypatch: pytest.MonkeyPatch):
    record = _record()
    assert maybe_failover(record, RateLimited("429"), enabled=False) is False
    assert record.account_name == "account-a"

    monkeypatch.setenv("WEBGPT_FAILOVER_ENABLED", "0")
    env_disabled = _record()
    assert maybe_failover(env_disabled, RateLimited("429")) is False
    assert env_disabled.account_name == "account-a"

    monkeypatch.setenv("WEBGPT_FAILOVER_ENABLED", "1")
    env_enabled = _record()
    assert maybe_failover(env_enabled, RateLimited("429")) is True


def test_retry_cap_one_failover_per_request(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WEBGPT_FAILOVER_ENABLED", raising=False)
    record = _record()
    assert maybe_failover(record, RateLimited("429"), attempts=0) is True
    # Second failure of the same request must not ping-pong A->B->A.
    record.account_name = "account-b"
    record.conversation_id = "conv-456"
    record.web_bootstrapped = True
    assert maybe_failover(record, RateLimited("429 again"), attempts=1) is False
    assert record.account_name == "account-b"
    assert record.conversation_id == "conv-456"


def test_unrelated_exceptions_never_fail_over():
    record = _record()
    assert maybe_failover(record, EmptyModelResponse("no text")) is False
    assert record.account_name == "account-a"
    assert record.conversation_id == "conv-123"


def test_failover_retry_required_maps_to_retryable_error():
    exc = FailoverRetryRequired("resend please")
    assert isinstance(exc, BrowserDisconnected)
    response = WebChatAPIServer._map_exception(exc)
    payload = response.headers
    assert payload["x-should-retry"] == "true"
    body = json.loads(bytes(response.body))
    assert body["error"]["retryable"] is True


class _FakeFactory:
    browser_manager = type("B", (), {"connected": True})()


class _LeaseFactory:
    def __init__(self, session):
        self.browser_manager = _FakeFactory.browser_manager
        self._session = session

    @asynccontextmanager
    async def lease(self):
        yield self._session


@pytest.mark.anyio
async def test_reconcile_verdict_fails_closed_without_history():
    server = WebChatAPIServer(mock_backend=True)
    record = _record(account_name=None)

    # Session without any reconcile capability.
    server._worker_factory = MultiAccountWorkerFactory({"a": _LeaseFactory(object())})
    assert await server._reconcile_user_turn_present(record) is None

    # Hybrid-style transport refuses to reconcile without history.
    class RaisingSession:
        async def reconcile(self, expected_user_text: str):
            raise CommitUnknown("cannot reconcile without history")

    server._worker_factory = MultiAccountWorkerFactory({"a": _LeaseFactory(RaisingSession())})
    assert await server._reconcile_user_turn_present(record) is None

    # Authoritative history proves the turn never landed -> explicit False.
    from types import SimpleNamespace

    class HistorySession:
        async def reconcile(self, expected_user_text: str):
            assert expected_user_text == record.pending_prompt
            return SimpleNamespace(user_turn_present=False)

    server._worker_factory = MultiAccountWorkerFactory({"a": _LeaseFactory(HistorySession())})
    assert await server._reconcile_user_turn_present(record) is False

    # No pending prompt -> nothing to reconcile about.
    empty = ConversationRecord()
    assert await server._reconcile_user_turn_present(empty) is None


@pytest.mark.anyio
async def test_server_failover_helper_multi_account_only(monkeypatch):
    server = WebChatAPIServer(mock_backend=True)

    # Single-account / mock path: never fail over here.
    record = _record()
    assert await server._maybe_failover_record(record, RateLimited("429")) is False
    assert record.account_name == "account-a"

    server._worker_factory = MultiAccountWorkerFactory({"a": _FakeFactory()})

    events: list[str] = []
    original_trace_emit = server.trace.emit

    def spy_emit(*args, **kwargs):
        result = original_trace_emit(*args, **kwargs)
        if args and len(args) >= 2:
            events.append(args[1])
        return result

    monkeypatch.setattr(server.trace, "emit", spy_emit)

    rate_record = _record()
    assert (
        await server._maybe_failover_record(rate_record, RateLimited("429")) is True
    )
    assert rate_record.account_name is None
    assert "conversation_failover" in events

    # CommitUnknown consults the reconcile verdict before deciding.
    async def _reconcile_absent(rec):
        return False

    monkeypatch.setattr(server, "_reconcile_user_turn_present", _reconcile_absent)
    commit_record = _record()
    commit_exc = CommitUnknown("uncertain", conversation_id="conv-123")
    assert (
        await server._maybe_failover_record(commit_record, commit_exc) is True
    )
    assert commit_record.account_name is None

    async def _reconcile_unavailable(rec):
        # The real helper converts any reconciliation failure into a
        # fail-closed ``None`` verdict.
        return None

    monkeypatch.setattr(server, "_reconcile_user_turn_present", _reconcile_unavailable)
    blocked = _record()
    assert await server._maybe_failover_record(blocked, commit_exc) is False
    assert blocked.account_name == "account-a"
