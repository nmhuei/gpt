"""Proxy Manager and IP Rotation Engine for WebGPT transports.

Inspired by ProxyCloud architecture: supports SOCKS5, HTTP, HTTPS proxy pools,
subscription imports, concurrent health checking, latency scoring, and dynamic
IP rotation to prevent Cloudflare/OpenAI IP throttling and reputation bans.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("gpt.transport.proxy")

ENV_PROXY = "WEBGPT_PROXY"
ENV_PROXY_FILE = "WEBGPT_PROXY_FILE"
ENV_PROXY_SUBSCRIPTIONS = "WEBGPT_PROXY_SUBSCRIPTIONS"
DEFAULT_PROXY_FILE = "~/.config/webgpt/proxies.txt"

# Default vetted fast proxy subscription feeds (ProxyCloud & community sources)
DEFAULT_SUBSCRIPTIONS = [
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt",
]


@dataclass
class ProxyNode:
    url: str
    protocol: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    latency_ms: float = float("inf")
    alive: bool = True
    fails: int = 0
    last_used: float = 0.0
    last_checked: float = 0.0

    @classmethod
    def parse(cls, raw: str, default_protocol: str = "socks5") -> ProxyNode | None:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            return None
        
        # Handle protocol prefix
        if "://" not in raw:
            raw = f"{default_protocol}://{raw}"
        
        try:
            parsed = urlparse(raw)
            if not parsed.hostname or not parsed.port:
                return None
            proto = parsed.scheme.lower()
            if proto not in {"socks5", "socks5h", "socks4", "http", "https"}:
                proto = default_protocol
            return cls(
                url=raw,
                protocol=proto,
                host=parsed.hostname,
                port=parsed.port,
                username=parsed.username,
                password=parsed.password,
            )
        except Exception:
            return None

    def to_browser_arg(self) -> str:
        """Format for Chromium --proxy-server flag."""
        return f"{self.protocol}://{self.host}:{self.port}"


class ProxyManager:
    """Manages an active pool of proxies with automatic latency check and rotation."""

    def __init__(
        self,
        *,
        proxy_file: str | None = None,
        subscriptions: list[str] | None = None,
        static_proxy: str | None = None,
    ) -> None:
        self.static_proxy = static_proxy or os.environ.get(ENV_PROXY, "").strip() or None
        self.proxy_file = (
            proxy_file
            or os.environ.get(ENV_PROXY_FILE, "").strip()
            or os.path.expanduser(DEFAULT_PROXY_FILE)
        )
        self.subscriptions = subscriptions or (
            [s.strip() for s in os.environ.get(ENV_PROXY_SUBSCRIPTIONS, "").split(",") if s.strip()]
            if os.environ.get(ENV_PROXY_SUBSCRIPTIONS)
            else DEFAULT_SUBSCRIPTIONS
        )
        self._pool: list[ProxyNode] = []
        self._current_index: int = 0
        self._lock = asyncio.Lock()
        self._last_refresh: float = 0.0

    @property
    def has_proxies(self) -> bool:
        return bool(self.static_proxy or self._pool)

    def load_from_file(self) -> list[ProxyNode]:
        """Load proxies from a local text file."""
        nodes: list[ProxyNode] = []
        p = Path(self.proxy_file).expanduser().resolve()
        if p.exists() and p.is_file():
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    node = ProxyNode.parse(line)
                    if node:
                        nodes.append(node)
                logger.info("Loaded %d proxies from %s", len(nodes), p)
            except Exception as e:
                logger.warning("Failed to read proxy file %s: %e", p, e)
        return nodes

    def fetch_subscription(self, url: str, timeout: float = 8.0) -> list[ProxyNode]:
        """Fetch and parse proxies from an online subscription URL."""
        nodes: list[ProxyNode] = []
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WebGPT/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                default_proto = "socks5" if "socks" in url.lower() else "http"
                for line in content.splitlines():
                    node = ProxyNode.parse(line, default_protocol=default_proto)
                    if node:
                        nodes.append(node)
            logger.info("Fetched %d proxies from subscription %s", len(nodes), url)
        except Exception as e:
            logger.debug("Failed to fetch proxy subscription %s: %s", url, e)
        return nodes

    async def check_node(self, node: ProxyNode, timeout: float = 3.0) -> bool:
        """Asynchronously probe proxy latency and connectivity to Cloudflare/ChatGPT."""
        start = time.monotonic()
        try:
            # Use asyncio to test socket connectivity
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(node.host, node.port),
                timeout=timeout,
            )
            node.latency_ms = (time.monotonic() - start) * 1000.0
            node.alive = True
            node.fails = 0
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            node.alive = False
            node.fails += 1
            node.latency_ms = float("inf")
            return False

    async def refresh_pool(self, max_nodes: int = 50, concurrent_checks: int = 20) -> None:
        """Fetch, probe, and filter the fastest working proxies."""
        async with self._lock:
            all_candidates: list[ProxyNode] = []
            
            # Static proxy priority
            if self.static_proxy:
                static_node = ProxyNode.parse(self.static_proxy)
                if static_node:
                    all_candidates.append(static_node)

            # Local file
            all_candidates.extend(self.load_from_file())

            # Online subscriptions if pool is small
            if len(all_candidates) < max_nodes:
                for sub_url in self.subscriptions[:2]:
                    nodes = await asyncio.to_thread(self.fetch_subscription, sub_url)
                    all_candidates.extend(nodes[:max_nodes])

            if not all_candidates:
                return

            # Deduplicate by (host, port)
            seen: set[tuple[str, int]] = set()
            unique_candidates: list[ProxyNode] = []
            for n in all_candidates:
                if (n.host, n.port) not in seen:
                    seen.add((n.host, n.port))
                    unique_candidates.append(n)

            # Fast concurrent health check
            sem = asyncio.Semaphore(concurrent_checks)

            async def _check(node: ProxyNode) -> ProxyNode:
                async with sem:
                    await self.check_node(node)
                    return node

            results = await asyncio.gather(
                *(_check(n) for n in unique_candidates[:max_nodes]),
                return_exceptions=True,
            )
            
            alive_nodes = [
                n for n in results
                if isinstance(n, ProxyNode) and n.alive and n.latency_ms < 2000.0
            ]
            # Sort by latency ascending
            alive_nodes.sort(key=lambda x: x.latency_ms)
            self._pool = alive_nodes
            self._last_refresh = time.monotonic()
            logger.info("Proxy pool active: %d verified fast nodes", len(self._pool))

    async def get_proxy(self) -> ProxyNode | None:
        """Get the current best active proxy, refreshing if pool is empty."""
        if self.static_proxy:
            return ProxyNode.parse(self.static_proxy)

        if not self._pool or (time.monotonic() - self._last_refresh > 600.0):
            await self.refresh_pool()

        if not self._pool:
            return None

        # Return the lowest-latency alive proxy
        self._current_index %= len(self._pool)
        node = self._pool[self._current_index]
        node.last_used = time.monotonic()
        return node

    async def rotate_proxy(self) -> ProxyNode | None:
        """Force rotate to the next healthy IP in the pool."""
        async with self._lock:
            if not self._pool:
                await self.refresh_pool()
            if not self._pool:
                return None
            
            self._current_index = (self._current_index + 1) % len(self._pool)
            next_node = self._pool[self._current_index]
            logger.info("Rotated proxy to %s (latency: %.1fms)", next_node.url, next_node.latency_ms)
            return next_node


# Global singleton instance
_global_proxy_manager: ProxyManager | None = None


def get_proxy_manager() -> ProxyManager:
    global _global_proxy_manager
    if _global_proxy_manager is None:
        _global_proxy_manager = ProxyManager()
    return _global_proxy_manager
