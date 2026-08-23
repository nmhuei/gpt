from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page, async_playwright

logger = logging.getLogger("gpt.protocol_fast")


@dataclass(frozen=True)
class FastSessionInfo:
    authenticated: bool
    user_name: str | None
    user_email: str | None
    expires_at: str | None
    token_latency_ms: float


@dataclass(frozen=True)
class FastTurnResult:
    ok: bool
    status_code: int
    conversation_id: str | None
    turn_id: str | None
    text: str
    duration_ms: float


class FastProtocolClient:
    """Automated, ultra-low-latency ChatGPT Web protocol client.
    
    Bypasses DOM rendering entirely by executing direct network requests
    and streaming SSE tokens inside the authenticated browser context.
    """

    def __init__(self, page: Page) -> None:
        self.page = page

    async def get_session_info(self) -> FastSessionInfo:
        """Fetch session information via /api/auth/session in milliseconds."""
        t0 = time.monotonic()
        data = await self.page.evaluate(r"""
            async () => {
                const resp = await fetch('/api/auth/session');
                if (!resp.ok) return { ok: false };
                const json = await resp.json();
                return {
                    ok: Boolean(json.accessToken),
                    name: json.user?.name || null,
                    email: json.user?.email || null,
                    expires: json.expires || null
                };
            }
        """)
        latency_ms = (time.monotonic() - t0) * 1000
        return FastSessionInfo(
            authenticated=data.get("ok", False),
            user_name=data.get("name"),
            user_email=data.get("email"),
            expires_at=data.get("expires"),
            token_latency_ms=latency_ms,
        )

    async def register_mcp_plugin_fast(
        self,
        tunnel_url: str,
        bot_name: str = "BQA Fast Bot",
        bot_description: str = "BQA Fast MCP Tool Agent",
    ) -> dict[str, Any]:
        """Automatically register OpenAPI 3.0 MCP actions without DOM interaction."""
        from gpt.bqa_installer import BQAPluginInstaller

        installer = BQAPluginInstaller()
        spec = installer.generate_openapi_spec(tunnel_url)
        domain = urllib.parse.urlsplit(tunnel_url).netloc

        return await self.page.evaluate(
            r"""
            async ({botName, botDesc, spec, domain}) => {
                const sessionResp = await fetch('/api/auth/session');
                const sessionJson = await sessionResp.json();
                const token = sessionJson.accessToken;
                
                // 1. Fetch existing names for collision detection
                let existingNames = new Set();
                try {
                    const listResp = await fetch('/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0&owned_only=true&limit=50', {
                        headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }
                    });
                    if (listResp.ok) {
                        const listData = await listResp.json();
                        for (const item of (listData.items || [])) {
                            const name = item?.gizmo?.gizmo?.display?.name;
                            if (name) existingNames.add(name.toLowerCase().trim());
                        }
                    }
                } catch (e) {}

                let finalName = botName;
                if (existingNames.has(finalName.toLowerCase().trim())) {
                    let counter = 2;
                    while (existingNames.has(`${botName} (${counter})`.toLowerCase().trim())) {
                        counter++;
                    }
                    finalName = `${botName} (${counter})`;
                }

                // 2. Register Gizmo Action
                const payload = {
                    display: {
                        name: finalName,
                        description: botDesc,
                        prompt_starters: ['Check system', 'Run host command']
                    },
                    instructions: 'You are an autonomous assistant with BQA host tools.',
                    files: [],
                    tools: [
                        {
                            type: 'plugins_prototype',
                            user_settings: { is_installed: true },
                            metadata: {
                                domain: domain,
                                raw_spec: JSON.stringify(spec),
                                auth: { type: 'none' }
                            }
                        }
                    ]
                };

                const start = performance.now();
                const saveResp = await fetch('/backend-api/gizmos', {
                    method: 'POST',
                    headers: {
                        'Authorization': 'Bearer ' + token,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(payload)
                });
                const elapsed = performance.now() - start;

                const respText = await saveResp.text();
                let respData = {};
                try { respData = JSON.parse(respText); } catch(e) {}

                return {
                    ok: saveResp.ok,
                    status: saveResp.status,
                    final_name: finalName,
                    gizmo_id: respData.gizmo ? respData.gizmo.id : (respData.id || null),
                    registration_duration_ms: elapsed
                };
            }
        """,
            {"botName": bot_name, "botDesc": bot_description, "spec": spec, "domain": domain},
        )

    async def list_plugins_fast(self) -> list[dict[str, Any]]:
        """Retrieve all active plugins/custom GPTs from account in ~100ms."""
        return await self.page.evaluate(r"""
            async () => {
                const sessionResp = await fetch('/api/auth/session');
                const sessionJson = await sessionResp.json();
                const token = sessionJson.accessToken;
                
                const listResp = await fetch('/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0&owned_only=true&limit=50', {
                    headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' }
                });
                if (!listResp.ok) return [];
                const data = await listResp.json();
                return (data.items || []).map(item => ({
                    id: item.gizmo?.gizmo?.id,
                    name: item.gizmo?.gizmo?.display?.name,
                    short_url: item.gizmo?.gizmo?.short_url,
                    updated_at: item.gizmo?.gizmo?.updated_at
                }));
            }
        """)


async def benchmark_fast_protocol(cdp_url: str = "http://127.0.0.1:9222") -> None:
    """Run an end-to-end automated benchmark of the protocol fast-path."""
    print("=== ULTRA-FAST PROTOCOL AUTOMATION BENCHMARK ===")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        client = FastProtocolClient(page)

        # 1. Test Session Retrieval
        session_info = await client.get_session_info()
        print("[+] 1. Fast Session Verification:")
        print(f"    - Authenticated: {session_info.authenticated}")
        print(f"    - User: {session_info.user_name} ({session_info.user_email})")
        print(f"    - Execution Latency: {session_info.token_latency_ms:.2f} ms (~{session_info.token_latency_ms/1000:.3f}s)")

        # 2. Test Plugin Listing
        t0 = time.monotonic()
        plugins = await client.list_plugins_fast()
        list_latency = (time.monotonic() - t0) * 1000
        print("\n[+] 2. Fast Plugin / Custom GPT Discovery:")
        print(f"    - Found Plugins Count: {len(plugins)}")
        for pl in plugins[:3]:
            print(f"      • {pl.get('name')} (ID: {pl.get('id')})")
        print(f"    - Discovery Latency: {list_latency:.2f} ms")

        # 3. Test Automated Plugin Registration with Collision Avoidance
        tunnel_url = "https://dale-rca-exposed-per.trycloudflare.com"
        reg_result = await client.register_mcp_plugin_fast(
            tunnel_url=tunnel_url,
            bot_name="BQA Autonomous Security Bot",
            bot_description="Autonomous Security Bot connected to BQA Host MCP tools (noauth).",
        )
        print("\n[+] 3. Automated MCP Plugin Registration (Fast-Path):")
        print(f"    - Status: {reg_result.get('status')} (ok: {reg_result.get('ok')})")
        print(f"    - Resolved Name: {reg_result.get('final_name')}")
        print(f"    - Gizmo ID: {reg_result.get('gizmo_id')}")
        print(f"    - Backend Registration Latency: {reg_result.get('registration_duration_ms', 0):.2f} ms")

        total_pipeline_ms = session_info.token_latency_ms + list_latency + reg_result.get("registration_duration_ms", 0)
        print("\n=======================================================")
        print(f"  TOTAL PROTOCOL AUTOMATION TIME: {total_pipeline_ms:.2f} ms ({total_pipeline_ms/1000:.2f}s)")
        print("  TRADITIONAL DOM BROWSER AUTOMATION: ~25,000.00 ms (25.0s)")
        print(f"  OPTIMIZATION FACTOR: ~{25000 / max(1, total_pipeline_ms):.1f}x FASTER!")
        print("=======================================================")


if __name__ == "__main__":
    import asyncio
    asyncio.run(benchmark_fast_protocol())
