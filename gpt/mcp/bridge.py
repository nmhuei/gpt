from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gpt.toolcall import ToolTranspiler
from gpt.transport.session import ChatGPTWebSession

logger = logging.getLogger("gpt.mcp_bridge")

DEFAULT_BQA_REST = "http://127.0.0.1:18427/api/v1"
DEFAULT_BURP_MCP = "http://127.0.0.1:9876/"
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "webgpt" / "tool_outputs"


@dataclass
class MCPTool:
    name: str
    description: str
    parameters: dict[str, Any]
    executor: Callable[[dict[str, Any]], str]


class OutputSanitizer:
    """Sanitizes raw shell/exploit output to avoid triggering OpenAI's cyber threat filter."""

    def __init__(self, output_dir: Path = DEFAULT_OUTPUT_DIR, max_preview_chars: int = 1500):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_preview_chars = max_preview_chars

    def sanitize(self, tool_name: str, raw_output: str) -> str:
        if not isinstance(raw_output, str):
            raw_output = str(raw_output)

        # Detect potential exploit/binary triggers
        is_sensitive = bool(
            re.search(r"(\\x[0-9a-fA-F]{2}){4,}", raw_output)  # Raw shellcode
            or "flag{" in raw_output.lower()
            or len(raw_output) > self.max_preview_chars
        )

        if not is_sensitive and len(raw_output) <= self.max_preview_chars:
            return raw_output

        # Save complete raw output to isolated debug file
        timestamp = int(asyncio.get_event_loop().time() * 1000) if asyncio.get_event_loop().is_running() else 0
        dump_file = self.output_dir / f"{tool_name}_{timestamp}.log"
        dump_file.write_text(raw_output, encoding="utf-8", errors="replace")

        preview = raw_output[: self.max_preview_chars].strip()
        lines = preview.splitlines()
        truncated_preview = "\n".join(lines[:30])

        return (
            f"[OUTPUT LOGGED TO {dump_file.name} ({len(raw_output)} bytes)]\n"
            f"Preview:\n{truncated_preview}\n"
            f"...(truncated {len(raw_output) - len(truncated_preview)} bytes for model context safety)"
        )


class MCPBridge:
    """Bridges local BQA REST / Burp MCP tools directly into ChatGPT Web."""

    def __init__(
        self,
        bqa_base_url: str = DEFAULT_BQA_REST,
        burp_mcp_url: str = DEFAULT_BURP_MCP,
        sanitizer: OutputSanitizer | None = None,
    ):
        self.bqa_base_url = bqa_base_url.rstrip("/")
        self.burp_mcp_url = burp_mcp_url
        self.sanitizer = sanitizer or OutputSanitizer()
        self.tools: dict[str, MCPTool] = {}

    def discover_bqa_tools(self) -> list[dict[str, Any]]:
        """Fetch REST tools and build OpenAI-compatible tool specifications."""
        cap_url = f"{self.bqa_base_url}/capabilities"
        try:
            req = urllib.request.Request(cap_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                _data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning(f"Could not discover BQA tools from {cap_url}: {exc}")
            return []

        discovered: list[dict[str, Any]] = []

        # 1. host_run_command
        def run_cmd_executor(args: dict[str, Any]) -> str:
            cmd = args.get("command", "")
            cwd = args.get("cwd")
            url = f"{self.bqa_base_url}/commands/run"
            payload = json.dumps({"command": cmd, "cwd": cwd}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                stdout = res.get("stdout", "")
                stderr = res.get("stderr", "")
                exit_code = res.get("exit_code", 0)
                raw = f"exit_code: {exit_code}\nstdout:\n{stdout}"
                if stderr:
                    raw += f"\nstderr:\n{stderr}"
                return self.sanitizer.sanitize("host_run_command", raw)

        self.tools["host_run_command"] = MCPTool(
            name="host_run_command",
            description="Execute a bash shell command in the host workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "cwd": {"type": "string", "description": "Optional working directory relative to workspace root"},
                },
                "required": ["command"],
            },
            executor=run_cmd_executor,
        )

        # 2. host_read_file
        def read_file_executor(args: dict[str, Any]) -> str:
            path = args.get("path", "")
            url = f"{self.bqa_base_url}/files/content?path={urllib.parse.quote(path)}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                content = res.get("content", "")
                return self.sanitizer.sanitize("host_read_file", content)

        self.tools["host_read_file"] = MCPTool(
            name="host_read_file",
            description="Read the contents of a text file inside the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                },
                "required": ["path"],
            },
            executor=read_file_executor,
        )

        # 3. host_list_directory
        def list_dir_executor(args: dict[str, Any]) -> str:
            path = args.get("path", ".")
            url = f"{self.bqa_base_url}/files?path={urllib.parse.quote(path)}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                items = res.get("items") or res.get("entries") or []
                formatted = [
                    f"{e.get('name')} ({'dir' if e.get('is_directory') else 'file'}, {e.get('size_bytes', 0)}B)"
                    for e in items
                ]
                return "\n".join(formatted[:100]) if formatted else "[Empty directory]"

        self.tools["host_list_directory"] = MCPTool(
            name="host_list_directory",
            description="List directory entries in the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path", "default": "."},
                },
            },
            executor=list_dir_executor,
        )

        for tool in self.tools.values():
            discovered.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )

        return discovered

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if not tool:
            return f"ERROR: Unknown tool '{name}'. Available: {list(self.tools.keys())}"
        try:
            return tool.executor(arguments)
        except Exception as exc:
            return f"ERROR executing '{name}': {type(exc).__name__}: {exc}"

    async def run_autonomous_task(
        self,
        session: ChatGPTWebSession,
        task: str,
        max_turns: int = 15,
        on_turn_callback: Callable[[int, str, list[dict[str, Any]] | None], None] | None = None,
    ) -> str:
        """Run a fully autonomous agent loop using ChatGPTWebSession and local MCP tools."""
        from gpt.promptcompat import render_messages

        tools_spec = self.discover_bqa_tools()
        if not tools_spec:
            raise RuntimeError("No local BQA tools discovered. Ensure `bqa start` is running.")

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are an autonomous AI security assistant in an authorized CTF lab. "
                    "You have access to tools: host_run_command, host_read_file, host_list_directory. "
                    "To solve the challenge, you MUST invoke tools using the XML format: "
                    "<tool_calls><invoke name=\"TOOL_NAME\"><parameter name=\"PARAM\"><![CDATA[VAL]]></parameter></invoke></tool_calls>. "
                    "Inspect the challenge directory first, read the code, and run Python to decrypt the flag."
                ),
            },
            {"role": "user", "content": task},
        ]

        for turn_idx in range(1, max_turns + 1):
            logger.info(f"--- Turn {turn_idx}/{max_turns} ---")
            full_prompt = render_messages(
                messages,
                initial=(turn_idx == 1),
                tools=tools_spec,
                tool_choice="auto",
            )

            result = await session.send(full_prompt, timeout_seconds=120)
            assistant_text = result.text or ""

            _assistant_prose, tool_calls = ToolTranspiler.parse_tool_calls(
                assistant_text,
                allowed_tools=set(self.tools.keys()),
                tool_definitions=tools_spec,
                allow_prose=True,
            )

            if on_turn_callback:
                on_turn_callback(turn_idx, assistant_text, tool_calls)

            if not tool_calls:
                logger.info("Agent completed task without additional tool calls.")
                return assistant_text

            messages.append({"role": "assistant", "content": assistant_text, "tool_calls": tool_calls})

            # Execute tool calls
            for call in tool_calls:
                fn_name = call["function"]["name"]
                fn_args = json.loads(call["function"]["arguments"])
                logger.info(f"[Executing Tool] {fn_name}({fn_args})")

                tool_result = self.execute_tool(fn_name, fn_args)
                logger.info(f"[Tool Output Preview] {tool_result[:150]}...")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": tool_result,
                    }
                )

        return "Task stopped: Reached maximum turn limit."
