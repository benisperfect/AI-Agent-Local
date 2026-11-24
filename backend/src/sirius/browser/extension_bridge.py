"""RPC Bridge to the Sirius Manifest V3 Browser Extension for live session control."""

from __future__ import annotations

import base64
from typing import Any

from sirius.browser.dom_distiller import DistilledPage
from sirius.ipc.server import IPCServer


class BrowserExtensionBridge:
    """Drives the user's running browser over authenticated WebSocket IPC without CDP restart."""

    def __init__(self, ipc: IPCServer) -> None:
        self._ipc = ipc

    @property
    def is_connected(self) -> bool:
        return self._ipc.has_extension_client

    async def list_tabs(self) -> list[dict[str, Any]]:
        res = await self._ipc.call_extension("list_tabs", {})
        tabs = res.get("tabs", [])
        return list(tabs) if isinstance(tabs, list) else []

    async def open_url(self, url: str, foreground: bool = True) -> dict[str, Any]:
        return await self._ipc.call_extension(
            "open_tab",
            {"url": url, "active": foreground},
        )

    async def distill_page(self, tab_id: int | None = None) -> DistilledPage:
        res = await self._ipc.call_extension("distill_dom", {"tab_id": tab_id})
        return DistilledPage.from_raw_dict(res)

    async def click(self, agent_id: str, tab_id: int | None = None) -> dict[str, Any]:
        return await self._ipc.call_extension(
            "click_element",
            {"agent_id": agent_id, "tab_id": tab_id},
        )

    async def fill(
        self,
        agent_id: str,
        text: str,
        press_enter: bool = False,
        tab_id: int | None = None,
    ) -> dict[str, Any]:
        return await self._ipc.call_extension(
            "fill_element",
            {
                "agent_id": agent_id,
                "text": text,
                "press_enter": press_enter,
                "tab_id": tab_id,
            },
        )

    async def select_option(
        self,
        agent_id: str,
        value: str,
        tab_id: int | None = None,
    ) -> dict[str, Any]:
        return await self._ipc.call_extension(
            "select_option",
            {"agent_id": agent_id, "value": value, "tab_id": tab_id},
        )

    async def scroll(
        self,
        dx: int,
        dy: int,
        tab_id: int | None = None,
    ) -> dict[str, Any]:
        return await self._ipc.call_extension(
            "scroll_page",
            {"dx": dx, "dy": dy, "tab_id": tab_id},
        )

    async def capture_screenshot_bytes(self) -> bytes:
        res = await self._ipc.call_extension("capture_visible_tab", {})
        data_url = str(res.get("data_url", ""))
        if "," in data_url:
            _, b64 = data_url.split(",", 1)
            return base64.b64decode(b64)
        return b""
