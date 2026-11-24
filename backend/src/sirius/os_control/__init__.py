"""Cross-platform 3-Tier Desktop & OS Control Engine for Sirius."""

from sirius.os_control.base import (
    AccessibilityNode,
    MediaAction,
    MouseButton,
    OSAdapter,
    PermissionReport,
    Rect,
    TilePosition,
    WindowInfo,
)
from sirius.os_control.controller import ComputerController, create_os_adapter
from sirius.os_control.mcp_bridge import MCPClientManager, MCPServerConfig
from sirius.os_control.shell_executor import ShellExecutionResult, StatefulShellExecutor

__all__ = [
    "AccessibilityNode",
    "ComputerController",
    "MCPClientManager",
    "MCPServerConfig",
    "MediaAction",
    "MouseButton",
    "OSAdapter",
    "PermissionReport",
    "Rect",
    "ShellExecutionResult",
    "StatefulShellExecutor",
    "TilePosition",
    "WindowInfo",
    "create_os_adapter",
]
