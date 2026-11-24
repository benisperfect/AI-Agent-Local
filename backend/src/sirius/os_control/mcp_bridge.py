"""Model Context Protocol (MCP) Client Manager for plugging external tool servers into Sirius."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import mcp
import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class MCPToolSpec:
    server_name: str
    tool_name: str
    qualified_name: str
    description: str
    input_schema: dict[str, Any]


class MCPClientManager:
    """Manages lifecycle, tool discovery, and invocation across external MCP servers."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._sessions: dict[str, mcp.ClientSession] = {}
        self._tools: dict[str, MCPToolSpec] = {}

    @property
    def discovered_tools(self) -> list[MCPToolSpec]:
        return list(self._tools.values())

    async def connect_server(self, config: MCPServerConfig) -> list[MCPToolSpec]:
        """Spawn stdio MCP server, initialize session, and index exposed tools."""
        params = mcp.StdioServerParameters(
            command=config.command,
            args=config.args,
            env=config.env,
        )
        read_stream, write_stream = await self._stack.enter_async_context(mcp.stdio_client(params))
        session = await self._stack.enter_async_context(
            mcp.ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._sessions[config.name] = session

        tools_res = await session.list_tools()
        added: list[MCPToolSpec] = []
        for t in tools_res.tools:
            qname = f"mcp__{config.name}__{t.name}"
            raw_schema = getattr(t, "input_schema", getattr(t, "inputSchema", None))
            schema = dict(raw_schema) if isinstance(raw_schema, dict) else {"type": "object"}
            spec = MCPToolSpec(
                server_name=config.name,
                tool_name=t.name,
                qualified_name=qname,
                description=t.description or f"MCP tool {t.name} from {config.name}",
                input_schema=schema,
            )
            self._tools[qname] = spec
            added.append(spec)

        logger.info("mcp_server_connected", server=config.name, tool_count=len(added))
        return added

    async def invoke_tool(
        self,
        qualified_name: str,
        arguments: dict[str, Any] | None = None,
        timeout_seconds: float = 25.0,
    ) -> dict[str, Any]:
        spec = self._tools.get(qualified_name)
        if spec is None:
            raise KeyError(f"Unknown MCP tool '{qualified_name}'")
        session = self._sessions.get(spec.server_name)
        if session is None:
            raise RuntimeError(f"MCP server '{spec.server_name}' is not connected.")

        result = await session.call_tool(
            name=spec.tool_name,
            arguments=arguments or {},
            read_timeout_seconds=timeout_seconds,
        )
        texts: list[str] = []
        content_items = getattr(result, "content", []) or []
        for item in content_items:
            txt = getattr(item, "text", None)
            if txt is not None:
                texts.append(str(txt))
            else:
                texts.append(str(item))
        is_err = bool(getattr(result, "isError", False))
        return {
            "ok": not is_err,
            "server": spec.server_name,
            "tool": spec.tool_name,
            "output": "\n".join(texts),
        }

    async def close_all(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()
        self._tools.clear()
