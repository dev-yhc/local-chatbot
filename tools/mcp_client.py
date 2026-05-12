from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from .registry import Tool

log = logging.getLogger(__name__)


class MCPManager:
    """Owns the lifetimes of stdio MCP server processes + ClientSessions.

    Kept as an explicit object so the FastAPI lifespan can hold it for the
    duration of the app, and so a future ReAct/Temporal layer can swap it
    out without touching the router.
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}
        self._started = False

    async def start_stack(self) -> None:
        await self._stack.__aenter__()
        self._started = True

    async def close(self) -> None:
        if self._started:
            await self._stack.__aexit__(None, None, None)
            self._started = False

    async def load(self, config_path: str) -> list[Tool]:
        path = Path(config_path)
        if not path.exists():
            return []

        try:
            config = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            log.warning("mcp config invalid JSON: %s", exc)
            return []

        servers = config.get("mcpServers", {})
        tools: list[Tool] = []
        for name, spec in servers.items():
            try:
                tools.extend(await self._connect(name, spec))
            except Exception as exc:
                # One bad server shouldn't take down the rest of the registry.
                log.warning("mcp server '%s' failed to start: %s", name, exc)
        return tools

    async def _connect(self, server_name: str, spec: dict[str, Any]) -> list[Tool]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=spec["command"],
            args=spec.get("args", []),
            env={**os.environ, **spec.get("env", {})},
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[server_name] = session

        listing = await session.list_tools()
        tools: list[Tool] = []
        for mcp_tool in listing.tools:
            qualified = f"{server_name}__{mcp_tool.name}"
            schema = {
                "type": "function",
                "function": {
                    "name": qualified,
                    "description": mcp_tool.description or f"{server_name}.{mcp_tool.name}",
                    "parameters": mcp_tool.inputSchema or {"type": "object", "properties": {}},
                },
            }
            tools.append(
                Tool(schema=schema, handler=self._make_handler(server_name, mcp_tool.name))
            )
        return tools

    def _make_handler(self, server_name: str, tool_name: str):
        async def handler(args: dict) -> str:
            session = self._sessions[server_name]
            result = await session.call_tool(tool_name, args)
            parts: list[str] = []
            for chunk in result.content:
                text = getattr(chunk, "text", None)
                parts.append(text if text is not None else str(chunk))
            return "\n".join(parts) if parts else "(no content)"

        return handler
