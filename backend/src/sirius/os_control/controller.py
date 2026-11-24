"""Unified 3-Tier ComputerController (Tier 1 Native -> Tier 2 A11y -> Tier 3 Vision)."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from sirius.ipc.protocol import ControlTier
from sirius.os_control.base import AccessibilityNode, OSAdapter
from sirius.os_control.linux_adapter import LinuxOSAdapter
from sirius.os_control.macos_adapter import MacOSAdapter
from sirius.os_control.mcp_bridge import MCPClientManager
from sirius.os_control.shell_executor import StatefulShellExecutor
from sirius.os_control.windows_adapter import WindowsOSAdapter

logger = structlog.get_logger(__name__)

VisionFallbackFn = Callable[[str, str, str | None], Awaitable[dict[str, Any]]]


def create_os_adapter() -> OSAdapter:
    """Instantiate the native OSAdapter for the running platform."""
    if sys.platform == "win32":
        return WindowsOSAdapter()
    if sys.platform == "darwin":
        return MacOSAdapter()
    return LinuxOSAdapter()


@dataclass(frozen=True)
class TierExecutionOutcome:
    ok: bool
    tier_used: ControlTier
    summary: str
    data: dict[str, Any]


class ComputerController:
    """Unified desktop controller selecting Tier 1 -> Tier 2 -> Tier 3 with graceful fallback."""

    def __init__(
        self,
        adapter: OSAdapter | None = None,
        shell: StatefulShellExecutor | None = None,
        mcp_manager: MCPClientManager | None = None,
        vision_fallback: VisionFallbackFn | None = None,
    ) -> None:
        self.adapter = adapter or create_os_adapter()
        self.shell = shell or StatefulShellExecutor()
        self.mcp = mcp_manager or MCPClientManager()
        self._vision_fallback = vision_fallback

    def attach_vision_fallback(self, handler: VisionFallbackFn) -> None:
        self._vision_fallback = handler

    def abort_all_immediate(self) -> None:
        """Emergency <100ms abort of active shell subprocesses."""
        self.shell.abort_active()

    async def inspect_ui_hierarchy(
        self,
        app_or_window: str | None = None,
        max_depth: int = 4,
    ) -> str:
        """Return compact semantic Tier-2 Accessibility tree text for LLM grounding."""
        roots = await self.adapter.get_accessibility_tree(
            app_or_window=app_or_window,
            max_depth=max_depth,
        )
        if not roots:
            return "(No semantic accessibility elements exposed; use Tier-3 Vision grounding.)"
        lines: list[str] = []
        for root in roots:
            lines.extend(root.to_compact_lines())
        return "\n".join(lines[:250])

    async def interact_ui(
        self,
        target_label: str,
        app_or_window: str | None = None,
        action: str = "click",
        value: str | None = None,
    ) -> TierExecutionOutcome:
        """Attempt Tier-2 Semantic Accessibility interaction first; fall back to Tier-3 Vision."""
        if app_or_window:
            await self.adapter.focus_window(app_or_window)

        roots = await self.adapter.get_accessibility_tree(
            app_or_window=app_or_window,
            max_depth=5,
        )
        matched = self._find_best_a11y_match(roots, target_label)
        if matched is not None:
            ok = await self.adapter.invoke_accessibility_element(
                node_id=matched.node_id,
                action=action,
                value=value,
            )
            if ok:
                logger.info(
                    "tier2_a11y_action_succeeded",
                    node_id=matched.node_id,
                    name=matched.name,
                    action=action,
                )
                return TierExecutionOutcome(
                    ok=True,
                    tier_used=ControlTier.TIER2_ACCESSIBILITY,
                    summary=f"Triggered '{matched.name}' ({matched.role}) via Accessibility API",
                    data={"node_id": matched.node_id, "role": matched.role, "name": matched.name},
                )

        # Automatic graceful fallback to Tier-3 Vision GUI Grounding
        if self._vision_fallback is not None:
            res = await self._vision_fallback(target_label, action, value)
            return TierExecutionOutcome(
                ok=bool(res.get("ok", False)),
                tier_used=ControlTier.TIER3_VISION,
                summary=str(res.get("summary", f"Vision action on '{target_label}'")),
                data=res,
            )

        return TierExecutionOutcome(
            ok=False,
            tier_used=ControlTier.TIER2_ACCESSIBILITY,
            summary=f"Could not locate UI element '{target_label}' in Accessibility tree and Vision fallback is not attached.",
            data={},
        )

    @staticmethod
    def _find_best_a11y_match(
        roots: list[AccessibilityNode],
        target_label: str,
    ) -> AccessibilityNode | None:
        needle = target_label.strip().lower()
        if not needle:
            return None

        best_node: AccessibilityNode | None = None
        best_score: int = 0

        for root in roots:
            for node in root.flatten():
                name_low = node.name.strip().lower()
                desc_low = node.description.strip().lower()
                score = 0
                if name_low == needle:
                    score = 100
                elif needle in name_low and name_low:
                    score = 75
                elif needle in desc_low and desc_low:
                    score = 55

                if score > 0 and node.actions:
                    score += 10
                if score > best_score:
                    best_score = score
                    best_node = node

        return best_node

    async def organize_or_search_files(
        self,
        operation: str,
        path_a: str,
        path_b: str = "",
        overwrite: bool = False,
    ) -> TierExecutionOutcome:
        if operation == "search":
            hits = await self.adapter.search_files(Path(path_a), path_b)
            return TierExecutionOutcome(
                ok=True,
                tier_used=ControlTier.TIER1_NATIVE,
                summary=f"Found {len(hits)} matching files under {path_a}",
                data={"matches": hits},
            )
        moved_to = await self.adapter.organize_file(
            Path(path_a), Path(path_b), overwrite=overwrite
        )
        return TierExecutionOutcome(
            ok=True,
            tier_used=ControlTier.TIER1_NATIVE,
            summary=f"Moved {path_a} -> {moved_to}",
            data={"destination": moved_to},
        )
