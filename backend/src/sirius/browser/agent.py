"""Playwright CDP + Companion Extension Hybrid Authenticated Browser Controller."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, Literal

import httpx
import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    Playwright,
    async_playwright,
)

from sirius.browser.dom_distiller import DistilledPage, get_dom_distiller_js
from sirius.browser.extension_bridge import BrowserExtensionBridge
from sirius.browser.profiles import BrowserKind, discover_browser_profile
from sirius.ipc.server import IPCServer

logger = structlog.get_logger(__name__)

BrowserExecutionMode = Literal["cdp", "extension", "disconnected"]


class AuthenticatedBrowserAgent:
    """Connects to the user's live logged-in browser (Brave by default) via CDP or MV3 Extension."""

    def __init__(
        self,
        ipc: IPCServer,
        primary_browser: BrowserKind = "brave",
        cdp_host: str = "127.0.0.1",
        cdp_port: int = 9222,
        downloads_dir: Path | None = None,
    ) -> None:
        self._ipc = ipc
        self._ext = BrowserExtensionBridge(ipc)
        self._primary_browser: BrowserKind = primary_browser
        self._cdp_host = cdp_host
        self._cdp_port = cdp_port
        self._downloads_dir = downloads_dir or (Path.home() / "Downloads")

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._owns_browser_process = False
        self._active_tab_index: int = 0
        self._background_pages: dict[str, Page] = {}
        self._recent_downloads: list[str] = []
        self._download_tasks: set[asyncio.Task[None]] = set()

    @property
    def primary_browser(self) -> BrowserKind:
        return self._primary_browser

    def set_primary_browser(self, kind: BrowserKind) -> None:
        self._primary_browser = kind

    @property
    def recent_downloads(self) -> list[str]:
        return list(self._recent_downloads)

    @property
    def mode(self) -> BrowserExecutionMode:
        if self._context is not None:
            return "cdp"
        if self._ext.is_connected:
            return "extension"
        return "disconnected"

    @property
    def is_connected(self) -> bool:
        return self.mode != "disconnected"

    async def attach_if_available(self) -> BrowserExecutionMode:
        """Attach to an existing CDP endpoint or connected MV3 companion extension without launching an empty browser."""
        if self._context is not None:
            return "cdp"

        cdp_endpoint = f"http://{self._cdp_host}:{self._cdp_port}"
        if await self._probe_cdp_endpoint(cdp_endpoint):
            await self._connect_cdp(cdp_endpoint)
            return "cdp"

        if self._ext.is_connected:
            return "extension"

        return "disconnected"

    async def launch_authenticated_cdp_browser(self) -> BrowserExecutionMode:
        """Launch the user's primary browser (e.g., Brave) with their real profile & CDP enabled."""
        mode = await self.attach_if_available()
        if mode != "disconnected":
            return mode

        profile = discover_browser_profile(self._primary_browser)
        if not profile.supports_cdp or not profile.executable_path:
            raise RuntimeError(
                f"{profile.display_name} cannot be launched with CDP automatically; "
                "install the Sirius Companion Extension in /browser-extension."
            )

        launch_args = profile.build_cdp_launch_args(self._cdp_port)
        await asyncio.create_subprocess_exec(
            *launch_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

        cdp_endpoint = f"http://{self._cdp_host}:{self._cdp_port}"
        for _ in range(25):
            await asyncio.sleep(0.25)
            if await self._probe_cdp_endpoint(cdp_endpoint):
                await self._connect_cdp(cdp_endpoint)
                return "cdp"

        raise RuntimeError(
            f"{profile.display_name} was already open without --remote-debugging-port={self._cdp_port}. "
            "Load /browser-extension in brave://extensions for zero-restart attachment, or restart Brave with CDP."
        )

    async def attach_playwright_context(self, context: BrowserContext) -> None:
        """Inject an existing Playwright BrowserContext (used by tests or custom persistent sessions)."""
        self._context = context
        for p in context.pages:
            self._wire_page_listeners(p)
        context.on("page", self._wire_page_listeners)

    async def close(self) -> None:
        if self._owns_browser_process and self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        self._browser = None
        self._context = None
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None

    async def _probe_cdp_endpoint(self, base_url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.2) as client:
                resp = await client.get(f"{base_url}/json/version")
                return resp.status_code == 200
        except Exception:
            return False

    async def _connect_cdp(self, endpoint: str) -> None:
        if self._pw is None:
            self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        if self._browser.contexts:
            self._context = self._browser.contexts[0]
        else:
            self._context = await self._browser.new_context()
        self._owns_browser_process = False
        for p in self._context.pages:
            self._wire_page_listeners(p)
        self._context.on("page", self._wire_page_listeners)
        logger.info("browser_attached_over_cdp", endpoint=endpoint, tabs=len(self._context.pages))

    def _wire_page_listeners(self, page: Page) -> None:
        def _on_download(download: Download) -> None:
            task = asyncio.create_task(self._save_download(download))
            self._download_tasks.add(task)
            task.add_done_callback(self._download_tasks.discard)

        page.on("download", _on_download)

    async def _save_download(self, download: Download) -> None:
        try:
            await asyncio.to_thread(self._downloads_dir.mkdir, parents=True, exist_ok=True)
            dest = self._downloads_dir / download.suggested_filename
            await download.save_as(str(dest))
            self._recent_downloads.append(str(dest))
            logger.info("browser_file_downloaded", path=str(dest))
        except Exception as exc:
            logger.warning("browser_download_save_failed", error=str(exc))

    def _get_cdp_page(self, tab_index: int | None = None) -> Page:
        if self._context is None or not self._context.pages:
            raise RuntimeError("No open tabs in attached Playwright CDP browser context.")
        pages = self._context.pages
        idx = self._active_tab_index if tab_index is None else tab_index
        idx = max(0, min(idx, len(pages) - 1))
        return pages[idx]

    # ------------------------------------------------------------------
    # Multi-Tab & Navigation API (Foreground + Silent Background Mode)
    # ------------------------------------------------------------------

    async def list_tabs(self) -> list[dict[str, Any]]:
        await self.attach_if_available()
        if self._context is not None:
            tabs: list[dict[str, Any]] = []
            for idx, p in enumerate(self._context.pages):
                with contextlib.suppress(Exception):
                    tabs.append(
                        {
                            "tab_index": idx,
                            "title": await p.title(),
                            "url": p.url,
                            "active": idx == self._active_tab_index,
                        }
                    )
            return tabs
        if self._ext.is_connected:
            return await self._ext.list_tabs()
        return []

    async def open_url(self, url: str, foreground: bool = True) -> dict[str, Any]:
        """Navigate or open a tab in either foreground (visible) or silent background mode."""
        await self.attach_if_available()
        normalized = url if "://" in url or url.startswith("about:") else f"https://{url}"

        if self._context is not None:
            page = await self._context.new_page()
            await page.goto(normalized, wait_until="domcontentloaded", timeout=25000)
            new_idx = self._context.pages.index(page)
            if foreground:
                self._active_tab_index = new_idx
                with contextlib.suppress(Exception):
                    await page.bring_to_front()
            else:
                self._background_pages[f"bg-{new_idx}"] = page

            return {
                "ok": True,
                "mode": "cdp",
                "foreground": foreground,
                "tab_index": new_idx,
                "url": page.url,
                "title": await page.title(),
            }

        if self._ext.is_connected:
            res = await self._ext.open_url(normalized, foreground=foreground)
            return {"ok": True, "mode": "extension", "foreground": foreground, **res}

        raise RuntimeError(
            "Browser is not attached yet. Launch Brave with CDP or enable the Sirius browser extension."
        )

    async def switch_tab(self, index_or_query: int | str) -> dict[str, Any]:
        await self.attach_if_available()
        if self._context is None:
            raise RuntimeError("CDP context required for direct tab index switching.")
        pages = self._context.pages
        chosen_idx = 0
        if isinstance(index_or_query, int):
            chosen_idx = max(0, min(index_or_query, len(pages) - 1))
        else:
            needle = index_or_query.lower()
            for idx, p in enumerate(pages):
                title = (await p.title()).lower()
                if needle in title or needle in p.url.lower():
                    chosen_idx = idx
                    break
        self._active_tab_index = chosen_idx
        target = pages[chosen_idx]
        with contextlib.suppress(Exception):
            await target.bring_to_front()
        return {"ok": True, "tab_index": chosen_idx, "url": target.url, "title": await target.title()}

    async def close_tab(self, tab_index: int | None = None) -> bool:
        if self._context is not None and self._context.pages:
            page = self._get_cdp_page(tab_index)
            await page.close()
            self._active_tab_index = max(0, min(self._active_tab_index, len(self._context.pages) - 1))
            return True
        return False

    # ------------------------------------------------------------------
    # Semantic DOM Distillation & Element Actions
    # ------------------------------------------------------------------

    async def distill_page(self, tab_index: int | None = None) -> DistilledPage:
        await self.attach_if_available()
        if self._context is not None:
            page = self._get_cdp_page(tab_index)
            raw = await page.evaluate(get_dom_distiller_js())
            return DistilledPage.from_raw_dict(raw if isinstance(raw, dict) else {})
        if self._ext.is_connected:
            return await self._ext.distill_page(tab_id=tab_index)
        raise RuntimeError("No authenticated browser session connected.")

    async def click(self, agent_id: str, tab_index: int | None = None) -> dict[str, Any]:
        await self.attach_if_available()
        if self._context is not None:
            page = self._get_cdp_page(tab_index)
            locator = page.locator(f'[data-agent-id="{agent_id}"]').first
            await locator.click(timeout=8000)
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            return {"ok": True, "agent_id": agent_id, "url": page.url}
        if self._ext.is_connected:
            return await self._ext.click(agent_id=agent_id, tab_id=tab_index)
        raise RuntimeError("No authenticated browser session connected.")

    async def fill(
        self,
        agent_id: str,
        text: str,
        press_enter: bool = False,
        tab_index: int | None = None,
    ) -> dict[str, Any]:
        await self.attach_if_available()
        if self._context is not None:
            page = self._get_cdp_page(tab_index)
            locator = page.locator(f'[data-agent-id="{agent_id}"]').first
            await locator.fill(text, timeout=8000)
            if press_enter:
                await locator.press("Enter")
            return {"ok": True, "agent_id": agent_id, "filled_length": len(text)}
        if self._ext.is_connected:
            return await self._ext.fill(
                agent_id=agent_id,
                text=text,
                press_enter=press_enter,
                tab_id=tab_index,
            )
        raise RuntimeError("No authenticated browser session connected.")

    async def select_dropdown(
        self,
        agent_id: str,
        value_or_label: str,
        tab_index: int | None = None,
    ) -> dict[str, Any]:
        await self.attach_if_available()
        if self._context is not None:
            page = self._get_cdp_page(tab_index)
            locator = page.locator(f'[data-agent-id="{agent_id}"]').first
            try:
                selected = await locator.select_option(value=value_or_label, timeout=4000)
            except Exception:
                selected = await locator.select_option(label=value_or_label, timeout=4000)
            return {"ok": True, "agent_id": agent_id, "selected": selected}
        if self._ext.is_connected:
            return await self._ext.select_option(agent_id, value_or_label, tab_id=tab_index)
        raise RuntimeError("No authenticated browser session connected.")

    async def upload_file(
        self,
        agent_id: str,
        file_path: str,
        tab_index: int | None = None,
    ) -> dict[str, Any]:
        if self._context is None:
            raise RuntimeError("File upload requires Playwright CDP browser attachment.")
        resolved = await asyncio.to_thread(lambda: Path(file_path).expanduser().resolve())
        exists = await asyncio.to_thread(resolved.exists)
        if not exists:
            raise FileNotFoundError(f"Upload file does not exist: {resolved}")
        page = self._get_cdp_page(tab_index)
        locator = page.locator(f'[data-agent-id="{agent_id}"]').first
        await locator.set_input_files(str(resolved))
        return {"ok": True, "agent_id": agent_id, "uploaded": str(resolved)}

    async def scroll(
        self,
        dx: int = 0,
        dy: int = 550,
        tab_index: int | None = None,
    ) -> dict[str, Any]:
        await self.attach_if_available()
        if self._context is not None:
            page = self._get_cdp_page(tab_index)
            await page.mouse.wheel(dx, dy)
            return {"ok": True, "dx": dx, "dy": dy}
        if self._ext.is_connected:
            return await self._ext.scroll(dx=dx, dy=dy, tab_id=tab_index)
        raise RuntimeError("No authenticated browser session connected.")

    async def capture_screenshot(self, tab_index: int | None = None) -> bytes:
        await self.attach_if_available()
        if self._context is not None:
            page = self._get_cdp_page(tab_index)
            return await page.screenshot(type="png", full_page=False)
        if self._ext.is_connected:
            return await self._ext.capture_screenshot_bytes()
        raise RuntimeError("No authenticated browser session connected.")
