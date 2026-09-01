from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

logger = logging.getLogger("gpt.bqa_installer")

DEFAULT_BQA_LOCAL_URL = "http://127.0.0.1:18427/api/v1"
DEFAULT_BQA_REPO_DIR = Path("/home/light/GitHub/botquanganh_mcp")
DEFAULT_LOGS_DIR = Path.home() / ".local" / "share" / "webgpt"


@dataclass(frozen=True)
class BQAPluginResult:
    ok: bool
    gizmo_id: str | None
    tunnel_url: str
    status_code: int
    bot_name: str
    detail: str


class BQAPluginInstaller:
    """Automates verification of BQA, Cloudflare tunnel creation, and Custom GPT Action registration."""

    def __init__(
        self,
        bqa_url: str = DEFAULT_BQA_LOCAL_URL,
        bqa_repo_dir: Path = DEFAULT_BQA_REPO_DIR,
        logs_dir: Path = DEFAULT_LOGS_DIR,
        cdp_url: str = "http://127.0.0.1:9222",
    ) -> None:
        self.bqa_url = bqa_url.rstrip("/")
        self.bqa_repo_dir = bqa_repo_dir
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.cdp_url = cdp_url

    def verify_and_ensure_bqa(self) -> bool:
        """Step 1: Check BQA health status, restart if offline."""
        logger.info("[Step 1] Verifying BQA local service status at %s...", self.bqa_url)
        try:
            req = urllib.request.Request(f"{self.bqa_url}/health", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("ok"):
                        logger.info("BQA service is ALIVE and healthy (version: %s)", data.get("version"))
                        return True
        except Exception as exc:
            logger.warning("BQA health check failed (%s). Attempting auto-restart...", exc)

        # Attempt start
        python_bin = self.bqa_repo_dir / ".venv" / "bin" / "python"
        if not python_bin.exists():
            python_bin = Path("python3")

        subprocess.Popen(
            [str(python_bin), "-m", "app.main"],
            cwd=str(self.bqa_repo_dir),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3.0)

        try:
            req = urllib.request.Request(f"{self.bqa_url}/health", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    logger.info("BQA service successfully restarted!")
                    return True
        except Exception as exc:
            logger.error("Failed to restart BQA service: %s", exc)
            return False
        return False

    def get_or_create_public_tunnel(self) -> str:
        """Step 2: Obtain or launch a public Cloudflare tunnel."""
        logger.info("[Step 2] Obtaining public Cloudflare tunnel URL...")
        log_file = self.logs_dir / "cloudflared.log"
        if log_file.exists():
            content = log_file.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                if "trycloudflare.com" in line:
                    for part in line.split():
                        if part.startswith("https://") and "trycloudflare.com" in part:
                            tunnel_url = part.strip()
                            try:
                                req = urllib.request.Request(
                                    f"{tunnel_url}/api/v1/health", headers={"Accept": "application/json"}
                                )
                                with urllib.request.urlopen(req, timeout=4) as resp:
                                    if resp.status == 200:
                                        logger.info("Reusing existing live tunnel: %s", tunnel_url)
                                        return tunnel_url
                            except Exception:
                                pass

        # Start fresh tunnel
        logger.info("Launching fresh cloudflared tunnel on port 18427...")
        subprocess.run(["pkill", "-f", "cloudflared"], check=False)
        time.sleep(1.0)
        with log_file.open("w", encoding="utf-8") as out:
            subprocess.Popen(
                ["/home/light/.local/bin/cloudflared", "tunnel", "--url", "http://127.0.0.1:18427"],
                stdout=out,
                stderr=out,
                start_new_session=True,
            )

        for _ in range(25):
            time.sleep(1.0)
            if log_file.exists():
                content = log_file.read_text(encoding="utf-8", errors="replace")
                for line in content.splitlines():
                    if "trycloudflare.com" in line:
                        for part in line.split():
                            if part.startswith("https://") and "trycloudflare.com" in part:
                                tunnel_url = part.strip()
                                logger.info("Acquired new public tunnel URL: %s", tunnel_url)
                                return tunnel_url

        raise RuntimeError("Timed out waiting for cloudflared tunnel to establish.")

    def generate_openapi_spec(self, tunnel_url: str) -> dict[str, Any]:
        """Build OpenAPI 3.0 specification for BQA tools."""
        return {
            "openapi": "3.0.0",
            "info": {
                "title": "BQA Host Tools",
                "description": "BQA Autonomous Command Execution and Filesystem Operations",
                "version": "1.0.0",
            },
            "servers": [{"url": f"{tunnel_url}/api/v1"}],
            "paths": {
                "/commands/run": {
                    "post": {
                        "summary": "Run a shell command on host",
                        "operationId": "host_run_command",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "command": {"type": "string", "description": "Command line to execute"},
                                            "cwd": {"type": "string", "description": "Working directory"},
                                            "timeout_seconds": {"type": "integer", "default": 60},
                                        },
                                        "required": ["command"],
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Command stdout, stderr and exit code",
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            }
                        },
                    }
                },
                "/files/read": {
                    "get": {
                        "summary": "Read file contents from host",
                        "operationId": "host_read_file",
                        "parameters": [
                            {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}}
                        ],
                        "responses": {
                            "200": {
                                "description": "File text and metadata",
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            }
                        },
                    }
                },
                "/files": {
                    "get": {
                        "summary": "List directory contents from host",
                        "operationId": "host_list_directory",
                        "parameters": [
                            {"name": "path", "in": "query", "required": False, "schema": {"type": "string", "default": "."}}
                        ],
                        "responses": {
                            "200": {
                                "description": "Directory items",
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            }
                        },
                    }
                },
            },
        }

    async def register_plugin(
        self,
        bot_name: str = "BQA Autonomous Security Bot",
        bot_description: str = "Autonomous Security Bot connected to BQA Host MCP tools (noauth).",
    ) -> BQAPluginResult:
        """Step 3: Register / Update Custom GPT Action on ChatGPT Web."""
        if not self.verify_and_ensure_bqa():
            return BQAPluginResult(
                ok=False,
                gizmo_id=None,
                tunnel_url="",
                status_code=500,
                bot_name=bot_name,
                detail="BQA local service is offline and failed to restart.",
            )

        tunnel_url = self.get_or_create_public_tunnel()
        openapi_spec = self.generate_openapi_spec(tunnel_url)
        domain = urllib.parse.urlsplit(tunnel_url).netloc

        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(self.cdp_url)
            except Exception:
                from gpt.debug import _find_cloak
                from gpt.profile import DEFAULT_CLOAK_PROFILE_DIR

                cloak_bin = _find_cloak(None)
                if cloak_bin:
                    subprocess.Popen(
                        [
                            cloak_bin,
                            f"--user-data-dir={DEFAULT_CLOAK_PROFILE_DIR}",
                            "--remote-debugging-port=9222",
                            "--no-first-run",
                            "https://chatgpt.com",
                        ],
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    time.sleep(3.0)
                browser = await p.chromium.connect_over_cdp(self.cdp_url)

            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()

            res = await page.evaluate(
                r"""
                async ({botName, botDesc, spec, domain}) => {
                    const sessionResp = await fetch('/api/auth/session');
                    const sessionJson = await sessionResp.json();
                    const token = sessionJson.accessToken;
                    
                    // Fetch existing gizmos to detect name collisions
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

                    // Resolve unique name if collision occurs
                    let finalName = botName;
                    if (existingNames.has(finalName.toLowerCase().trim())) {
                        let counter = 2;
                        while (existingNames.has(`${botName} (${counter})`.toLowerCase().trim())) {
                            counter++;
                        }
                        finalName = `${botName} (${counter})`;
                    }

                    const gizmoPayload = {
                        display: {
                            name: finalName,
                            description: botDesc,
                            prompt_starters: ['Check system status', 'Analyze CTF challenge', 'Run host command']
                        },
                        instructions: 'You are BQA Autonomous Security Bot. You have access to BQA Host Tools to run shell commands, inspect files, and analyze security challenges. Use the tools whenever the user requests execution or inspection.',
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
                    
                    const saveResp = await fetch('/backend-api/gizmos', {
                        method: 'POST',
                        headers: {
                            'Authorization': 'Bearer ' + token,
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify(gizmoPayload)
                    });
                    
                    const respText = await saveResp.text();
                    let respData = {};
                    try { respData = JSON.parse(respText); } catch(e) {}
                    
                    return {
                        ok: saveResp.ok,
                        status: saveResp.status,
                        final_name: finalName,
                        was_renamed: finalName !== botName,
                        gizmo_id: respData.gizmo ? respData.gizmo.id : (respData.id || null),
                        detail: respData
                    };
                }
            """,
                {"botName": bot_name, "botDesc": bot_description, "spec": openapi_spec, "domain": domain},
            )

        return BQAPluginResult(
            ok=res.get("ok", False),
            gizmo_id=res.get("gizmo_id"),
            tunnel_url=tunnel_url,
            status_code=res.get("status", 0),
            bot_name=res.get("final_name", bot_name),
            detail=json.dumps(res.get("detail", {})),
        )
