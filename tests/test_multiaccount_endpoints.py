"""Smoke tests for API endpoints running against a multi-account factory.

Regression coverage for the codex finding: ``/health`` and ``/v1/models``
reached ``factory.browser_manager`` directly, which ``MultiAccountWorkerFactory``
does not have, so multi-account deployments 500'd on those endpoints.
"""

from contextlib import asynccontextmanager

import pytest
from starlette.testclient import TestClient

from gpt.api.server import create_api_app
from gpt.conversations import ConversationRecord
from gpt.transport.factory import ChatGPTWorkerFactory, WorkerFactoryStats
from gpt.transport.multi_account import MultiAccountWorkerFactory


class StubBrowserManager:
    def __init__(self, connected: bool = True):
        self.connected = connected


class FakeSession:
    def __init__(self, account_name: str):
        self.state_value = "ready"
        self.auth_status = "authenticated"
        self.account_name = account_name

    @property
    def state(self):
        class _State:
            value = self.state_value

        return _State()

    @property
    def browser_manager(self):
        stub = StubBrowserManager(connected=True)

        class _Mgr:
            connected = stub.connected

        return _Mgr()

    @property
    def ui_driver(self):
        outer = self

        class _Driver:
            async def auth_status(self):
                return outer.auth_status

        return _Driver()

    async def models(self):
        return []


class FakeAccountFactory:
    """Per-account factory double exposing the same surface the server uses."""

    def __init__(self, name: str, connected: bool = True):
        self.name = name
        self.browser_manager = StubBrowserManager(connected=connected)
        self.started = False
        self.close_calls = 0
        self.lease_calls: list[str | None] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.close_calls += 1

    async def stats(self) -> WorkerFactoryStats:
        return WorkerFactoryStats(
            max_workers=1,
            live_workers=1,
            idle_workers=0,
            leased_workers=0,
            queue_waiters=0,
            created_workers=1,
            closed_workers=0,
        )

    @asynccontextmanager
    async def lease(self, affinity_key: str | None = None):
        self.lease_calls.append(affinity_key)
        yield FakeSession(self.name)


def _install_multiaccount_factory(server):
    factories = {
        "alpha": FakeAccountFactory("alpha"),
        "beta": FakeAccountFactory("beta"),
    }
    server._worker_factory = MultiAccountWorkerFactory(factories)
    return factories


def test_health_reports_ready_browser_for_multiaccount(tmp_path):
    app = create_api_app(headless=True, account_profiles={"a": str(tmp_path / "a")})
    server = app.state.server
    factories = _install_multiaccount_factory(server)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    # Regression: this used to raise AttributeError -> HTTP 500.
    assert payload["browser"] == "ready"
    assert payload["workers"]["max"] == 2
    assert all(factory.started for factory in factories.values())


def test_models_endpoint_does_not_500_for_multiaccount(tmp_path):
    app = create_api_app(headless=True, account_profiles={"a": str(tmp_path / "a")})
    server = app.state.server
    _install_multiaccount_factory(server)

    with TestClient(app) as client:
        response = client.get("/v1/models")

    assert response.status_code == 200
    model_ids = [item["id"] for item in response.json()["data"]]
    assert "chatgpt-web" in model_ids


def test_readiness_uses_aggregated_connectivity_hybrid_multiaccount(tmp_path):
    app = create_api_app(
        headless=True,
        transport="hybrid",
        account_profiles={"only": str(tmp_path / "p")},
    )
    server = app.state.server
    factory = FakeAccountFactory("only")
    server._worker_factory = MultiAccountWorkerFactory({"only": factory})

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["browser"] == "ready"
    assert payload["backend"] == "ready"


async def test_multiaccount_start_rolls_back_started_factories_on_failure():
    good = FakeAccountFactory("good")
    bad_start = FakeAccountFactory("bad")

    async def exploding_start():
        raise RuntimeError("second account bootstrap failed")

    bad_start.start = exploding_start

    factory = MultiAccountWorkerFactory({"good": good, "bad": bad_start})

    with pytest.raises(RuntimeError, match="second account bootstrap failed"):
        await factory.start()

    # The already-started first account must be closed again (no browser leak).
    assert good.started is True
    assert good.close_calls == 1


async def test_multiaccount_start_rolls_back_factory_that_opened_then_raised():
    """start() mở tài nguyên xong mới nổ → chính nó vẫn phải được close().

    Regression: factory từng nằm ngoài tập rollback vì append diễn ra sau
    khi start() thành công, nên tài nguyên nó vừa mở bị leak.
    """
    good = FakeAccountFactory("good")
    halfway = FakeAccountFactory("halfway")

    async def open_resource_then_raise():
        halfway.started = True  # giả lập đã mở browser/resource rồi mới nổ
        raise RuntimeError("opened resource then exploded")

    halfway.start = open_resource_then_raise

    factory = MultiAccountWorkerFactory({"good": good, "boom": halfway})

    with pytest.raises(RuntimeError, match="opened resource then exploded"):
        await factory.start()

    # Factory nổ giữa chừng nằm trong tập rollback và bị close đúng 1 lần,
    # cùng với các factory đã start thành công trước đó.
    assert halfway.close_calls == 1
    assert good.close_calls == 1


async def test_browsers_connected_reflects_underlying_managers():
    healthy = FakeAccountFactory("healthy")
    disconnected = FakeAccountFactory("down", connected=False)

    both_up = MultiAccountWorkerFactory({"a": healthy, "b": FakeAccountFactory("c")})
    one_down = MultiAccountWorkerFactory({"a": healthy, "b": disconnected})

    assert both_up.browsers_connected is True
    assert one_down.browsers_connected is False


def test_chatgpt_worker_factory_exposes_browsers_connected():
    factory = ChatGPTWorkerFactory(StubBrowserManager(connected=True))
    assert factory.browsers_connected is True
    factory_down = ChatGPTWorkerFactory(StubBrowserManager(connected=False))
    assert factory_down.browsers_connected is False


async def test_lease_session_affinity_chosen_by_signature_not_by_typeerror():
    """A TypeError from the turn body must propagate, never trigger re-lease."""
    app = create_api_app(headless=True)
    server = app.state.server
    factory = FakeAccountFactory("alpha")
    server._worker_factory = factory
    record = ConversationRecord(session_id="sess-1", conversation_id="conv-1")

    with pytest.raises(TypeError, match="boom inside turn body"):
        async with server._lease_session(record):
            raise TypeError("boom inside turn body")

    # Exactly one lease attempt carrying the affinity key; no legacy retry.
    assert factory.lease_calls == ["conv-1"]


async def test_lease_session_falls_back_to_plain_lease_without_affinity_support():
    """Hybrid-style lease() (no affinity param) keeps working via signature probe."""

    class NoAffinityFactory:
        def __init__(self):
            self.session = FakeSession("hybrid")
            self.lease_calls = 0

        async def stats(self):
            return WorkerFactoryStats(1, 0, 0, 0, 0, 0, 0)

        @asynccontextmanager
        async def lease(self):
            self.lease_calls += 1
            yield self.session

    app = create_api_app(headless=True)
    server = app.state.server
    factory = NoAffinityFactory()
    server._worker_factory = factory
    record = ConversationRecord(session_id="sess-9", conversation_id="conv-9")

    async with server._lease_session(record) as session:
        assert session.account_name == "hybrid"

    assert factory.lease_calls == 1
