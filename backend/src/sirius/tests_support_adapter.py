"""Deterministic Recording OSAdapter for automated E2E orchestrator verification."""

from __future__ import annotations

from typing import Any

from sirius.os_control.base import (
    AccessibilityNode,
    MediaAction,
    MouseButton,
    OSAdapter,
    PermissionReport,
    ScriptLanguage,
    TilePosition,
    WindowInfo,
)


class RecordingOSAdapter(OSAdapter):
    """Records invoked OS calls in memory without altering host desktop settings during tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._volume = 50
        self._clip = ""

    @property
    def os_name(self) -> str:
        return "RecordingOS"

    async def list_windows(self) -> list[WindowInfo]:
        return []

    async def launch_app(self, app_name_or_path: str, args: list[str] | None = None) -> str:
        self.calls.append(("launch_app", app_name_or_path))
        return f"Launched {app_name_or_path}"

    async def focus_window(self, title_or_app: str) -> bool:
        self.calls.append(("focus_window", title_or_app))
        return True

    async def minimize_window(self, title_or_app: str) -> bool:
        self.calls.append(("minimize_window", title_or_app))
        return True

    async def tile_window(self, title_or_app: str, position: TilePosition) -> bool:
        self.calls.append(("tile_window", (title_or_app, position)))
        return True

    async def close_window(self, title_or_app: str) -> bool:
        self.calls.append(("close_window", title_or_app))
        return True

    async def get_volume(self) -> int:
        return self._volume

    async def set_volume(self, percent: int) -> bool:
        self._volume = percent
        self.calls.append(("set_volume", percent))
        return True

    async def set_mute(self, muted: bool) -> bool:
        self.calls.append(("set_mute", muted))
        return True

    async def set_brightness(self, percent: int) -> bool:
        self.calls.append(("set_brightness", percent))
        return True

    async def set_wifi(self, enabled: bool) -> bool:
        self.calls.append(("set_wifi", enabled))
        return True

    async def set_bluetooth(self, enabled: bool) -> bool:
        self.calls.append(("set_bluetooth", enabled))
        return True

    async def media_control(self, action: MediaAction) -> bool:
        self.calls.append(("media_control", action))
        return True

    async def send_notification(self, title: str, body: str) -> bool:
        self.calls.append(("send_notification", (title, body)))
        return True

    async def get_clipboard(self) -> str:
        return self._clip

    async def set_clipboard(self, text: str) -> bool:
        self._clip = text
        return True

    async def run_os_script(self, script: str, language: ScriptLanguage) -> str:
        self.calls.append(("run_os_script", (language, script)))
        return "ok"

    async def get_accessibility_tree(
        self,
        app_or_window: str | None = None,
        max_depth: int = 5,
    ) -> list[AccessibilityNode]:
        return []

    async def invoke_accessibility_element(
        self,
        node_id: str,
        action: str = "click",
        value: str | None = None,
    ) -> bool:
        self.calls.append(("invoke_a11y", (node_id, action, value)))
        return True

    async def move_mouse(self, x: int, y: int, smooth: bool = True) -> None:
        self.calls.append(("move_mouse", (x, y)))

    async def click(
        self,
        x: int,
        y: int,
        button: MouseButton = "left",
        count: int = 1,
    ) -> None:
        self.calls.append(("click", (x, y, button, count)))

    async def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: MouseButton = "left",
    ) -> None:
        self.calls.append(("drag", (start_x, start_y, end_x, end_y)))

    async def scroll(self, dx: int, dy: int, x: int | None = None, y: int | None = None) -> None:
        self.calls.append(("scroll", (dx, dy)))

    async def press_hotkey(self, keys: list[str]) -> None:
        self.calls.append(("press_hotkey", keys))

    async def type_text(self, text: str) -> None:
        self.calls.append(("type_text", text))

    async def check_permissions(self) -> PermissionReport:
        return PermissionReport(True, True, True, True)
