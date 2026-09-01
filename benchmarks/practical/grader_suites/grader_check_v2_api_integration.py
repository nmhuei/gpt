import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

import pytest

from feedclient import AuthError, FeedClient, FeedError, InvalidResponse, iterate_items


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _ready(url):
    for _ in range(100):
        try:
            with urlopen(url + "/healthz", timeout=0.2):
                return
        except Exception:
            time.sleep(0.02)
    raise RuntimeError("mock server not ready")


@contextmanager
def server(scenario):
    external = os.environ.get("WB_BASE_URL")
    if external and os.environ.get("WB_SCENARIO") == scenario:
        yield external
        return
    port = _free_port()
    env = dict(os.environ)
    env["SCENARIO"] = scenario
    work = Path.cwd()
    proc = subprocess.Popen(
        [sys.executable, str(work / "mockserver" / "server.py"), str(port)],
        cwd=work / "mockserver",
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _ready(url)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)


def stats(url):
    with urlopen(url + "/__debug/stats", timeout=1) as response:
        return json.load(response)


def test_integration_happy():
    with server("happy") as url:
        client = FeedClient(url, "secret", sleep=lambda _: None)
        page = client.fetch_page()
        assert len(page["items"]) == 3
        assert page["next_cursor"] is None


def test_integration_paginated():
    with server("paginated") as url:
        got = list(iterate_items(FeedClient(url, "secret", sleep=lambda _: None)))
        ids = [item["id"] for item in got]
        assert len(ids) == 250
        assert len(set(ids)) == 250
        assert ids[0] == "0" and ids[-1] == "249"


def test_integration_ratelimited():
    with server("ratelimited") as url:
        sleeps = []
        client = FeedClient(url, "secret", sleep=sleeps.append)
        assert len(client.fetch_page()["items"]) == 3
        assert sleeps == [3.0]
        assert stats(url)["items"] == 2


def test_integration_flaky500():
    with server("flaky500") as url:
        sleeps = []
        client = FeedClient(url, "secret", sleep=sleeps.append)
        assert len(client.fetch_page()["items"]) == 3
        assert sleeps == [0.1, 0.2]
        assert stats(url)["items"] == 3


def test_integration_unauthorized():
    with server("unauthorized") as url:
        sleeps = []
        client = FeedClient(url, "wrong", sleep=sleeps.append)
        with pytest.raises(AuthError):
            client.fetch_page()
        assert sleeps == []
        assert stats(url)["items"] == 1


def test_integration_badjson():
    with server("badjson") as url:
        client = FeedClient(url, "secret", sleep=lambda _: None)
        with pytest.raises(InvalidResponse) as excinfo:
            client.fetch_page()
        assert excinfo.value.status == 200


def test_invalid_limit_not_retried():
    with server("happy") as url:
        client = FeedClient(url, "secret", sleep=lambda _: None)
        with pytest.raises(FeedError):
            client.fetch_page(limit=101)
        assert stats(url)["items"] == 1


def test_subscribe_requires_idempotency_key():
    with server("happy") as url:
        client = FeedClient(url, "secret", sleep=lambda _: None)
        with pytest.raises(FeedError):
            client.subscribe("news")
        assert stats(url)["subscribe"] == 1


def test_subscribe_success():
    with server("happy") as url:
        client = FeedClient(url, "secret", sleep=lambda _: None)
        assert client.subscribe("news", "k-1") == {"ok": True}
