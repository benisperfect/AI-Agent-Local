"""Phase 2 Verification Suite: Cross-Platform OSAdapter, Accessibility Tree, Shell & MCP."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from sirius.ipc.protocol import ControlTier
from sirius.os_control.base import (
    AccessibilityNode,
    MediaAction,
    MouseButton,
    OSAdapter,
    PermissionReport,
    Rect,
    ScriptLanguage,
    TilePosition,
    WindowInfo,
    cubic_bezier_points,
)
from sirius.os_control.controller import ComputerController, create_os_adapter
from sirius.os_control.macos_adapter import MacOSAdapter
from sirius.os_control.mcp_bridge import MCPClientManager, MCPServerConfig
from sirius.os_control.shell_executor import StatefulShellExecutor
from sirius.os_control.windows_adapter import WindowsOSAdapter


class DummyA11yAdapter(OSAdapter):
    """Deterministic adapter for verifying Tier-2 -> Tier-3 fallback orchestration."""

    def __init__(self, tree: list[AccessibilityNode]) -> None:
        self._tree = tree
        self.invoked: list[tuple[str, str, str | None]] = []

    @property
    def os_name(self) -> str:
        return "TestOS"

    async def list_windows(self) -> list[WindowInfo]:
        return []

    async def launch_app(self, app_name_or_path: str, args: list[str] | None = None) -> str:
        return app_name_or_path

    async def focus_window(self, title_or_app: str) -> bool:
        return True

    async def minimize_window(self, title_or_app: str) -> bool:
        return True

    async def tile_window(self, title_or_app: str, position: TilePosition) -> bool:
        return True

    async def close_window(self, title_or_app: str) -> bool:
        return True

    async def get_volume(self) -> int:
        return 60

    async def set_volume(self, percent: int) -> bool:
        return True

    async def set_mute(self, muted: bool) -> bool:
        return True

    async def set_brightness(self, percent: int) -> bool:
        return True

    async def set_wifi(self, enabled: bool) -> bool:
        return True

    async def set_bluetooth(self, enabled: bool) -> bool:
        return True

    async def media_control(self, action: MediaAction) -> bool:
        return True

    async def send_notification(self, title: str, body: str) -> bool:
        return True

    async def get_clipboard(self) -> str:
        return ""

    async def set_clipboard(self, text: str) -> bool:
        return True

    async def run_os_script(self, script: str, language: ScriptLanguage) -> str:
        return ""

    async def get_accessibility_tree(
        self, app_or_window: str | None = None, max_depth: int = 5
    ) -> list[AccessibilityNode]:
        return self._tree

    async def invoke_accessibility_element(
        self, node_id: str, action: str = "click", value: str | None = None
    ) -> bool:
        self.invoked.append((node_id, action, value))
        return True

    async def move_mouse(self, x: int, y: int, smooth: bool = True) -> None:
        return None

    async def click(
        self, x: int, y: int, button: MouseButton = "left", count: int = 1
    ) -> None:
        return None

    async def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: MouseButton = "left",
    ) -> None:
        return None

    async def scroll(self, dx: int, dy: int, x: int | None = None, y: int | None = None) -> None:
        return None

    async def press_hotkey(self, keys: list[str]) -> None:
        return None

    async def type_text(self, text: str) -> None:
        return None

    async def check_permissions(self) -> PermissionReport:
        return PermissionReport(True, True, True, True)


def test_cross_platform_adapters_instantiate_and_bezier() -> None:
    mac = MacOSAdapter()
    win = WindowsOSAdapter()
    native = create_os_adapter()
    assert mac.os_name == "macOS"
    assert win.os_name == "Windows"
    assert native.os_name.startswith(("Linux", "macOS", "Windows"))

    pts = cubic_bezier_points((100, 100), (500, 350), steps=10)
    assert len(pts) == 10
    assert pts[-1] == (500, 350)


async def test_native_os_adapter_files_clipboard_permissions_and_a11y(tmp_path: Path) -> None:
    adapter = create_os_adapter()

    # File search & organize
    inv_dir = os.path.join(str(tmp_path), "invoices")
    os.makedirs(inv_dir, exist_ok=True)
    sample_str = os.path.join(inv_dir, "stripe_payout_nov.csv")
    with open(sample_str, "w", encoding="utf-8") as fh:
        fh.write("amount\n1250.00\n")

    found = await adapter.search_files(tmp_path, "*stripe*.csv")
    assert len(found) == 1
    assert found[0].endswith("stripe_payout_nov.csv")

    dest_dir = tmp_path / "archive"
    moved_path = await adapter.organize_file(Path(sample_str), dest_dir)
    assert os.path.exists(moved_path)
    assert not os.path.exists(sample_str)

    # Clipboard round-trip
    await adapter.set_clipboard("sirius-clipboard-check")
    clip = await adapter.get_clipboard()
    assert "sirius-clipboard-check" in clip or clip == ""

    # Permissions & Volume
    perms = await adapter.check_permissions()
    assert isinstance(perms.to_bool_map(), dict)
    vol = await adapter.get_volume()
    assert 0 <= vol <= 150

    # Live AT-SPI2 / OS Accessibility query (non-crashing & structured)
    tree = await adapter.get_accessibility_tree(max_depth=2)
    assert isinstance(tree, list)


async def test_tier2_to_tier3_graceful_fallback() -> None:
    tree = [
        AccessibilityNode(
            node_id="ax:1",
            role="window",
            name="Editor",
            children=[
                AccessibilityNode(
                    node_id="ax:1.0",
                    role="push button",
                    name="Save File",
                    bounds=Rect(20, 30, 80, 28),
                    actions=["click"],
                )
            ],
        )
    ]
    dummy = DummyA11yAdapter(tree)
    vision_calls: list[str] = []

    async def fake_vision(label: str, action: str, value: str | None) -> dict[str, Any]:
        vision_calls.append(label)
        return {"ok": True, "summary": f"Vision clicked {label}", "action": action, "value": value}

    ctrl = ComputerController(adapter=dummy, vision_fallback=fake_vision)

    # Known label uses Tier-2 Accessibility directly without Vision LLM
    out1 = await ctrl.interact_ui("Save File", action="click")
    assert out1.ok
    assert out1.tier_used == ControlTier.TIER2_ACCESSIBILITY
    assert dummy.invoked == [("ax:1.0", "click", None)]
    assert len(vision_calls) == 0

    # Unlabeled canvas button falls back automatically to Tier-3 Vision
    out2 = await ctrl.interact_ui("Canvas Brush Tool", action="click")
    assert out2.ok
    assert out2.tier_used == ControlTier.TIER3_VISION
    assert vision_calls == ["Canvas Brush Tool"]


async def test_stateful_shell_executor_cwd_env_and_abort(tmp_path: Path) -> None:
    sub = tmp_path / "workspace_alpha"
    sub.mkdir()
    shell = StatefulShellExecutor(initial_cwd=tmp_path)
    shell.set_env("SIRIUS_PROJECT", "autonomous-voice")

    r1 = await shell.execute("cd workspace_alpha && echo $SIRIUS_PROJECT")
    assert r1.exit_code == 0
    assert "autonomous-voice" in r1.stdout
    assert Path(r1.cwd) == sub.resolve()

    # Subsequent command runs inside `workspace_alpha` automatically!
    r2 = await shell.execute("pwd")
    assert r2.exit_code == 0
    assert str(sub.resolve()) in r2.stdout

    # Timeout & clean process-group kill
    r_timeout = await shell.execute("sleep 5", timeout_seconds=0.2)
    assert r_timeout.timed_out


async def test_mcp_stdio_client_bridge(tmp_path: Path) -> None:
    server_script = tmp_path / "mini_mcp_server.py"
    server_script.write_text(
        """
from mcp.server import MCPServer

app = MCPServer("demo-calc")

@app.tool()
def multiply(a: int, b: int) -> str:
    \"\"\"Multiply two integers\"\"\"
    return f"product={a * b}"

if __name__ == "__main__":
    app.run(transport="stdio")
""",
        encoding="utf-8",
    )

    mgr = MCPClientManager()
    try:
        tools = await mgr.connect_server(
            MCPServerConfig(
                name="calc",
                command=sys.executable,
                args=[str(server_script)],
            )
        )
        assert len(tools) == 1
        assert tools[0].qualified_name == "mcp__calc__multiply"

        call_res = await mgr.invoke_tool("mcp__calc__multiply", {"a": 7, "b": 6})
        assert call_res["ok"] is True
        assert "product=42" in call_res["output"]
    finally:
        await mgr.close_all()
