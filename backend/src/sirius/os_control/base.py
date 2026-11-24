"""Abstract OSAdapter interface and cross-platform UI/Accessibility data structures."""

from __future__ import annotations

import abc
import asyncio
import fnmatch
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

MouseButton = Literal["left", "right", "middle"]
TilePosition = Literal["left", "right", "maximize", "restore"]
MediaAction = Literal["play_pause", "next", "previous", "stop"]
ScriptLanguage = Literal["powershell", "applescript", "jxa", "shortcuts", "dbus", "bash"]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + max(self.width, 0) // 2, self.y + max(self.height, 0) // 2)

    def is_visible(self) -> bool:
        return self.width > 1 and self.height > 1


@dataclass(frozen=True)
class WindowInfo:
    window_id: str
    title: str
    app_name: str
    pid: int
    bounds: Rect
    focused: bool = False
    minimized: bool = False


@dataclass
class AccessibilityNode:
    node_id: str
    role: str
    name: str
    value: str = ""
    description: str = ""
    bounds: Rect | None = None
    actions: list[str] = field(default_factory=list)
    enabled: bool = True
    focused: bool = False
    children: list[AccessibilityNode] = field(default_factory=list)

    def to_compact_lines(self, indent: int = 0) -> list[str]:
        prefix = "  " * indent
        coords = (
            f" @({self.bounds.x},{self.bounds.y},{self.bounds.width}x{self.bounds.height})"
            if self.bounds and self.bounds.is_visible()
            else ""
        )
        val_part = f" val={self.value!r}" if self.value else ""
        act_part = f" actions={self.actions}" if self.actions else ""
        line = f"{prefix}[{self.node_id}] {self.role}: {self.name!r}{val_part}{coords}{act_part}"
        lines = [line]
        for child in self.children:
            lines.extend(child.to_compact_lines(indent + 1))
        return lines

    def flatten(self) -> list[AccessibilityNode]:
        items = [self]
        for child in self.children:
            items.extend(child.flatten())
        return items


@dataclass(frozen=True)
class PermissionReport:
    microphone: bool
    accessibility: bool
    screen_recording: bool
    input_synthesis: bool
    details: dict[str, str] = field(default_factory=dict)

    def to_bool_map(self) -> dict[str, bool]:
        return {
            "microphone": self.microphone,
            "accessibility": self.accessibility,
            "screen_recording": self.screen_recording,
            "input_synthesis": self.input_synthesis,
        }


def cubic_bezier_points(
    start: tuple[int, int],
    end: tuple[int, int],
    steps: int = 12,
) -> list[tuple[int, int]]:
    """Generate smooth human-like eased cubic Bézier cursor trajectory coordinates."""
    if steps <= 1 or start == end:
        return [end]
    sx, sy = float(start[0]), float(start[1])
    ex, ey = float(end[0]), float(end[1])
    dx, dy = ex - sx, ey - sy
    # Subtle natural arc control points
    c1x, c1y = sx + dx * 0.28 - dy * 0.06, sy + dy * 0.28 + dx * 0.06
    c2x, c2y = sx + dx * 0.74 - dy * 0.03, sy + dy * 0.74 + dx * 0.03

    pts: list[tuple[int, int]] = []
    for idx in range(1, steps + 1):
        t = idx / float(steps)
        u = 1.0 - t
        px = (u**3) * sx + 3 * (u**2) * t * c1x + 3 * u * (t**2) * c2x + (t**3) * ex
        py = (u**3) * sy + 3 * (u**2) * t * c1y + 3 * u * (t**2) * c2y + (t**3) * ey
        pts.append((round(px), round(py)))
    return pts


class OSAdapter(abc.ABC):
    """Cross-platform contract for Tier-1 OS APIs, Tier-2 Accessibility, and Tier-3 Input."""

    @property
    @abc.abstractmethod
    def os_name(self) -> str:
        """Return descriptive OS & session identifier."""

    # --- Tier 1: App & Window Control ---
    @abc.abstractmethod
    async def list_windows(self) -> list[WindowInfo]: ...

    @abc.abstractmethod
    async def launch_app(self, app_name_or_path: str, args: list[str] | None = None) -> str: ...

    @abc.abstractmethod
    async def focus_window(self, title_or_app: str) -> bool: ...

    @abc.abstractmethod
    async def minimize_window(self, title_or_app: str) -> bool: ...

    @abc.abstractmethod
    async def tile_window(self, title_or_app: str, position: TilePosition) -> bool: ...

    @abc.abstractmethod
    async def close_window(self, title_or_app: str) -> bool: ...

    # --- Tier 1: System Settings & Media ---
    @abc.abstractmethod
    async def get_volume(self) -> int: ...

    @abc.abstractmethod
    async def set_volume(self, percent: int) -> bool: ...

    @abc.abstractmethod
    async def set_mute(self, muted: bool) -> bool: ...

    @abc.abstractmethod
    async def set_brightness(self, percent: int) -> bool: ...

    @abc.abstractmethod
    async def set_wifi(self, enabled: bool) -> bool: ...

    @abc.abstractmethod
    async def set_bluetooth(self, enabled: bool) -> bool: ...

    @abc.abstractmethod
    async def media_control(self, action: MediaAction) -> bool: ...

    @abc.abstractmethod
    async def send_notification(self, title: str, body: str) -> bool: ...

    # --- Tier 1: Clipboard & OS Scripting ---
    @abc.abstractmethod
    async def get_clipboard(self) -> str: ...

    @abc.abstractmethod
    async def set_clipboard(self, text: str) -> bool: ...

    @abc.abstractmethod
    async def run_os_script(self, script: str, language: ScriptLanguage) -> str: ...

    # --- Tier 1: Cross-Platform File Search & Organization (shared concrete helpers) ---
    async def search_files(
        self,
        root: Path,
        query: str,
        max_results: int = 40,
    ) -> list[str]:
        """Fast case-insensitive glob/substring file search skipping hidden cache trees."""
        return await asyncio.to_thread(self._search_files_sync, root, query, max_results)

    @staticmethod
    def _search_files_sync(root: Path, query: str, max_results: int) -> list[str]:
        base = root.expanduser().resolve()
        if not base.exists():
            return []
        pattern = query.lower()
        has_glob = any(ch in pattern for ch in ("*", "?", "["))
        ignored_dirs = {".git", "node_modules", ".venv", "__pycache__", ".cache", "Library"}
        matches: list[str] = []

        for current_root, dirs, files in base.walk():
            dirs[:] = [d for d in dirs if d not in ignored_dirs]
            for name in files + dirs:
                low = name.lower()
                matched = fnmatch.fnmatch(low, pattern) if has_glob else (pattern in low)
                if matched:
                    matches.append(str(current_root / name))
                    if len(matches) >= max_results:
                        return matches
        return matches

    async def organize_file(
        self,
        source: Path,
        destination: Path,
        overwrite: bool = False,
    ) -> str:
        return await asyncio.to_thread(self._organize_file_sync, source, destination, overwrite)

    @staticmethod
    def _organize_file_sync(source: Path, destination: Path, overwrite: bool) -> str:
        src = source.expanduser().resolve()
        dst = destination.expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(f"Source file does not exist: {src}")
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists() and not overwrite:
            raise FileExistsError(f"Destination already exists and overwrite=False: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        moved = shutil.move(str(src), str(dst))
        return str(moved)

    # --- Tier 2: Semantic Accessibility Tree ---
    @abc.abstractmethod
    async def get_accessibility_tree(
        self,
        app_or_window: str | None = None,
        max_depth: int = 5,
    ) -> list[AccessibilityNode]: ...

    @abc.abstractmethod
    async def invoke_accessibility_element(
        self,
        node_id: str,
        action: str = "click",
        value: str | None = None,
    ) -> bool: ...

    # --- Tier 3: Cross-Platform Input Synthesis ---
    @abc.abstractmethod
    async def move_mouse(self, x: int, y: int, smooth: bool = True) -> None: ...

    @abc.abstractmethod
    async def click(
        self,
        x: int,
        y: int,
        button: MouseButton = "left",
        count: int = 1,
    ) -> None: ...

    @abc.abstractmethod
    async def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: MouseButton = "left",
    ) -> None: ...

    @abc.abstractmethod
    async def scroll(self, dx: int, dy: int, x: int | None = None, y: int | None = None) -> None: ...

    @abc.abstractmethod
    async def press_hotkey(self, keys: list[str]) -> None: ...

    @abc.abstractmethod
    async def type_text(self, text: str) -> None: ...

    # --- Permissions & Readiness ---
    @abc.abstractmethod
    async def check_permissions(self) -> PermissionReport: ...
