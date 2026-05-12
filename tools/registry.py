from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class Tool:
    schema: dict[str, Any]
    handler: ToolHandler

    @property
    def name(self) -> str:
        return self.schema["function"]["name"]


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema for t in self.tools.values()]

    def names(self) -> list[str]:
        return list(self.tools.keys())

    def is_empty(self) -> bool:
        return not self.tools

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'. Available: {self.names()}"
        try:
            return await tool.handler(arguments)
        except Exception as exc:
            log.exception("tool %s failed", name)
            return f"Error calling tool '{name}': {exc}"


async def build_registry(mcp_config_path: str):
    """Wire up available tools. Returns (registry, mcp_manager-or-None).

    Each backend degrades independently: missing TAVILY_API_KEY → no web search,
    missing/empty mcp.json → no MCP tools, but the caller still gets a registry
    (possibly empty) so the chat loop can decide whether to expose tools at all.
    """
    from .mcp_client import MCPManager
    from .web_search import build_web_search_tool

    registry = ToolRegistry()

    web_tool = build_web_search_tool()
    if web_tool is not None:
        registry.add(web_tool)
        log.info("web_search enabled (Tavily)")
    else:
        log.info("web_search disabled (TAVILY_API_KEY missing)")

    if not Path(mcp_config_path).exists():
        log.info("MCP disabled (%s not found)", mcp_config_path)
        return registry, None

    manager = MCPManager()
    await manager.start_stack()
    mcp_tools = await manager.load(mcp_config_path)

    if not mcp_tools:
        log.info("MCP config present but no usable tools — shutting down stack")
        await manager.close()
        return registry, None

    for tool in mcp_tools:
        registry.add(tool)
    log.info("MCP enabled with %d tools: %s", len(mcp_tools), [t.name for t in mcp_tools])
    return registry, manager
