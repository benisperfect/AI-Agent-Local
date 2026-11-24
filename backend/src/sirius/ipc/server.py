"""Authenticated localhost WebSocket IPC Server for Sirius Daemon."""

from __future__ import annotations

import asyncio
import hmac
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import ValidationError
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from sirius import __version__
from sirius.ipc.protocol import (
    AuthClientMessage,
    AuthOkMessage,
    ClientMessage,
    ClientRole,
    ErrorMessage,
    ExtensionCommandMessage,
    ExtensionResultMessage,
    ServerMessage,
    parse_client_message,
)

logger = structlog.get_logger(__name__)

MessageHandler = Callable[[ClientMessage, str], Awaitable[None]]


@dataclass
class ConnectedClient:
    session_id: str
    role: ClientRole
    connection: ServerConnection
    metadata: dict[str, Any] = field(default_factory=dict)


class IPCServer:
    """Localhost-bound authenticated WebSocket IPC hub."""

    def __init__(
        self,
        host: str,
        port: int,
        auth_token: str,
        on_message: MessageHandler | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._on_message = on_message
        self._server: Server | None = None
        self._clients: dict[str, ConnectedClient] = {}
        self._pending_extension_rpcs: dict[str, asyncio.Future[ExtensionResultMessage]] = {}

    @property
    def bound_port(self) -> int:
        if self._server and self._server.sockets:
            sockname = self._server.sockets[0].getsockname()
            return int(sockname[1])
        return self._port

    @property
    def has_extension_client(self) -> bool:
        return any(c.role == "browser_extension" for c in self._clients.values())

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._on_message = handler

    async def start(self) -> None:
        """Start listening on localhost for authenticated IPC connections."""
        self._server = await serve(
            self._handle_connection,
            host=self._host,
            port=self._port,
            max_size=8 * 1024 * 1024,  # 8MB for page DOM snapshots / base64 frames
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info("ipc_server_started", host=self._host, port=self.bound_port)

    async def stop(self) -> None:
        """Gracefully close all active IPC connections and shut down server."""
        for fut in self._pending_extension_rpcs.values():
            if not fut.done():
                fut.cancel()
        self._pending_extension_rpcs.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._clients.clear()
        logger.info("ipc_server_stopped")

    async def _handle_connection(self, ws: ServerConnection) -> None:
        session_id = uuid.uuid4().hex[:12]
        try:
            raw_first = await asyncio.wait_for(ws.recv(), timeout=5.0)
            msg = parse_client_message(raw_first)
            if not isinstance(msg, AuthClientMessage):
                await self._send_one(
                    ws,
                    ErrorMessage(
                        code="AUTH_REQUIRED",
                        message="First frame must be an auth message.",
                        recoverable=False,
                    ),
                )
                await ws.close(code=4401, reason="Authentication required")
                return

            if not hmac.compare_digest(msg.token.encode("utf-8"), self._auth_token.encode("utf-8")):
                logger.warning("ipc_auth_failed", session_id=session_id, role=msg.client_role)
                await self._send_one(
                    ws,
                    ErrorMessage(
                        code="INVALID_TOKEN",
                        message="Invalid IPC authentication token.",
                        recoverable=False,
                    ),
                )
                await ws.close(code=4403, reason="Invalid token")
                return

            client = ConnectedClient(
                session_id=session_id,
                role=msg.client_role,
                connection=ws,
                metadata=msg.metadata,
            )
            self._clients[session_id] = client
            logger.info("ipc_client_authenticated", session_id=session_id, role=msg.client_role)

            await self._send_one(
                ws,
                AuthOkMessage(
                    client_role=msg.client_role,
                    daemon_version=__version__,
                    session_id=session_id,
                ),
            )

            async for raw_frame in ws:
                try:
                    parsed = parse_client_message(raw_frame)
                except ValidationError as exc:
                    await self._send_one(
                        ws,
                        ErrorMessage(
                            code="SCHEMA_VALIDATION_ERROR",
                            message=str(exc),
                            recoverable=True,
                        ),
                    )
                    continue

                if isinstance(parsed, ExtensionResultMessage):
                    fut = self._pending_extension_rpcs.pop(parsed.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(parsed)
                    continue

                if self._on_message is not None:
                    await self._on_message(parsed, session_id)

        except TimeoutError:
            await ws.close(code=4408, reason="Auth handshake timeout")
        except ValidationError:
            await ws.close(code=4400, reason="Malformed auth message")
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.exception("ipc_connection_error", session_id=session_id, error=str(exc))
        finally:
            removed = self._clients.pop(session_id, None)
            if removed is not None:
                logger.info("ipc_client_disconnected", session_id=session_id, role=removed.role)

    async def _send_one(self, ws: ServerConnection, message: ServerMessage) -> None:
        try:
            await ws.send(message.model_dump_json())
        except ConnectionClosed:
            pass

    async def broadcast(
        self,
        message: ServerMessage,
        roles: set[ClientRole] | None = None,
    ) -> None:
        """Broadcast a ServerMessage to all matching authenticated clients."""
        targets = [
            c.connection
            for c in list(self._clients.values())
            if roles is None or c.role in roles
        ]
        if not targets:
            return
        serialized = message.model_dump_json()
        await asyncio.gather(
            *(self._safe_send_raw(conn, serialized) for conn in targets),
            return_exceptions=True,
        )

    async def send_to_session(self, session_id: str, message: ServerMessage) -> None:
        client = self._clients.get(session_id)
        if client is not None:
            await self._send_one(client.connection, message)

    async def call_extension(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float = 12.0,
    ) -> dict[str, Any]:
        """Dispatch an RPC request to an attached Manifest V3 browser extension client."""
        ext_clients = [c for c in self._clients.values() if c.role == "browser_extension"]
        if not ext_clients:
            raise RuntimeError("No authenticated browser extension client is connected.")

        target = ext_clients[-1]
        request_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ExtensionResultMessage] = loop.create_future()
        self._pending_extension_rpcs[request_id] = fut

        cmd = ExtensionCommandMessage(
            request_id=request_id,
            action=action,
            payload=payload or {},
        )
        try:
            await self._send_one(target.connection, cmd)
            result = await asyncio.wait_for(fut, timeout=timeout_seconds)
            if not result.ok:
                raise RuntimeError(result.error or f"Browser extension action '{action}' failed.")
            return result.data
        finally:
            self._pending_extension_rpcs.pop(request_id, None)

    @staticmethod
    async def _safe_send_raw(ws: ServerConnection, payload: str) -> None:
        try:
            await ws.send(payload)
        except ConnectionClosed:
            pass
