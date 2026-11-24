"""macOS 13+ (Ventura / Sonoma / Sequoia) OSAdapter implementation for Sirius."""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

import psutil
import structlog

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

logger = structlog.get_logger(__name__)


class MacOSAdapter(OSAdapter):
    """Production macOS adapter using AppleScript/JXA, ApplicationServices AXUIElement, and Quartz."""

    def __init__(self) -> None:
        self._cursor_pos: tuple[int, int] = (500, 500)
        self._ax_refs: dict[str, Any] = {}
        self._ax_bounds: dict[str, Rect] = {}

    @property
    def os_name(self) -> str:
        return "macOS"

    async def _exec(self, *args: str, stdin_bytes: bytes | None = None) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate(stdin_bytes)
        return (
            proc.returncode or 0,
            out_b.decode("utf-8", errors="replace").strip(),
            err_b.decode("utf-8", errors="replace").strip(),
        )

    async def _osascript(self, script: str, lang: str = "AppleScript") -> tuple[int, str, str]:
        return await self._exec("osascript", "-l", lang, "-e", script)

    # ------------------------------------------------------------------
    # Tier 1: App & Window Control
    # ------------------------------------------------------------------

    async def list_windows(self) -> list[WindowInfo]:
        jxa = r"""
        const se = Application("System Events");
        const out = [];
        for (const proc of se.applicationProcesses.whose({ backgroundOnly: false })()) {
            const appName = proc.name();
            const pid = proc.unixId();
            const focused = proc.frontmost();
            try {
                for (let i = 0; i < proc.windows.length; i++) {
                    const w = proc.windows[i];
                    const pos = w.position();
                    const sz = w.size();
                    out.push({
                        window_id: `${pid}:${i}`,
                        title: w.name() || appName,
                        app_name: appName,
                        pid: pid,
                        x: pos[0], y: pos[1], width: sz[0], height: sz[1],
                        focused: focused && i === 0
                    });
                }
            } catch (e) {}
        }
        JSON.stringify(out);
        """
        rc, out, _ = await self._osascript(jxa, lang="JavaScript")
        if rc != 0 or not out:
            return []
        try:
            items = json.loads(out)
        except json.JSONDecodeError:
            return []

        return [
            WindowInfo(
                window_id=str(it["window_id"]),
                title=str(it["title"]),
                app_name=str(it["app_name"]),
                pid=int(it["pid"]),
                bounds=Rect(
                    x=int(it["x"]),
                    y=int(it["y"]),
                    width=int(it["width"]),
                    height=int(it["height"]),
                ),
                focused=bool(it.get("focused", False)),
            )
            for it in items
        ]

    async def launch_app(self, app_name_or_path: str, args: list[str] | None = None) -> str:
        cmd = ["open", "-a", app_name_or_path]
        if args:
            cmd.extend(["--args", *args])
        rc, _, err = await self._exec(*cmd)
        if rc != 0:
            raise RuntimeError(err or f"Failed to launch macOS app '{app_name_or_path}'")
        return f"Launched {app_name_or_path}"

    async def focus_window(self, title_or_app: str) -> bool:
        safe = title_or_app.replace('"', '\\"')
        rc, _, _ = await self._osascript(f'tell application "{safe}" to activate')
        return rc == 0

    async def minimize_window(self, title_or_app: str) -> bool:
        safe = title_or_app.replace('"', '\\"')
        script = (
            f'tell application "System Events" to tell process "{safe}" '
            "to set miniaturized of front window to true"
        )
        rc, _, _ = await self._osascript(script)
        return rc == 0

    async def tile_window(self, title_or_app: str, position: TilePosition) -> bool:
        safe = title_or_app.replace('"', '\\"')
        jxa = f"""
        ObjC.import('AppKit');
        const frame = $.NSScreen.mainScreen.visibleFrame;
        const sw = frame.size.width;
        const sh = frame.size.height;
        const sx = frame.origin.x;
        const sy = 25;
        const se = Application("System Events");
        const proc = se.processes.byName("{safe}");
        const win = proc.windows[0];
        if ("{position}" === "left") {{
            win.position = [sx, sy];
            win.size = [Math.floor(sw / 2), sh];
        }} else if ("{position}" === "right") {{
            win.position = [sx + Math.floor(sw / 2), sy];
            win.size = [Math.floor(sw / 2), sh];
        }} else if ("{position}" === "maximize") {{
            win.position = [sx, sy];
            win.size = [sw, sh];
        }} else {{
            win.position = [sx + 120, sy + 80];
            win.size = [Math.floor(sw * 0.75), Math.floor(sh * 0.75)];
        }}
        "ok";
        """
        rc, _, _ = await self._osascript(jxa, lang="JavaScript")
        return rc == 0

    async def close_window(self, title_or_app: str) -> bool:
        safe = title_or_app.replace('"', '\\"')
        rc, _, _ = await self._osascript(f'tell application "{safe}" to quit')
        return rc == 0

    # ------------------------------------------------------------------
    # Tier 1: System Settings & Media
    # ------------------------------------------------------------------

    async def get_volume(self) -> int:
        rc, out, _ = await self._osascript("output volume of (get volume settings)")
        return int(out) if rc == 0 and out.isdigit() else 50

    async def set_volume(self, percent: int) -> bool:
        clamped = max(0, min(100, percent))
        rc, _, _ = await self._osascript(f"set volume output volume {clamped}")
        return rc == 0

    async def set_mute(self, muted: bool) -> bool:
        val = "true" if muted else "false"
        rc, _, _ = await self._osascript(f"set volume output muted {val}")
        return rc == 0

    async def set_brightness(self, percent: int) -> bool:
        clamped = max(5, min(100, percent)) / 100.0
        if shutil.which("brightness"):
            rc, _, _ = await self._exec("brightness", f"{clamped:.2f}")
            return rc == 0
        return False

    async def set_wifi(self, enabled: bool) -> bool:
        state = "on" if enabled else "off"
        rc, _, _ = await self._exec("networksetup", "-setairportpower", "en0", state)
        return rc == 0

    async def set_bluetooth(self, enabled: bool) -> bool:
        if shutil.which("blueutil"):
            rc, _, _ = await self._exec("blueutil", "--power", "1" if enabled else "0")
            return rc == 0
        return False

    async def media_control(self, action: MediaAction) -> bool:
        cmd_map = {
            "play_pause": "playpause",
            "next": "next track",
            "previous": "previous track",
            "stop": "pause",
        }
        script = f'tell application "Music" to {cmd_map[action]}'
        rc, _, _ = await self._osascript(script)
        return rc == 0

    async def send_notification(self, title: str, body: str) -> bool:
        safe_t = title.replace('"', '\\"')
        safe_b = body.replace('"', '\\"')
        rc, _, _ = await self._osascript(f'display notification "{safe_b}" with title "{safe_t}"')
        return rc == 0

    async def get_clipboard(self) -> str:
        rc, out, _ = await self._exec("pbpaste")
        return out if rc == 0 else ""

    async def set_clipboard(self, text: str) -> bool:
        rc, _, _ = await self._exec("pbcopy", stdin_bytes=text.encode("utf-8"))
        return rc == 0

    async def run_os_script(self, script: str, language: ScriptLanguage) -> str:
        if language == "applescript":
            rc, out, err = await self._osascript(script, lang="AppleScript")
            if rc != 0:
                raise RuntimeError(err)
            return out
        if language == "jxa":
            rc, out, err = await self._osascript(script, lang="JavaScript")
            if rc != 0:
                raise RuntimeError(err)
            return out
        if language == "shortcuts":
            rc, out, err = await self._exec("shortcuts", "run", script)
            if rc != 0:
                raise RuntimeError(err)
            return out
        if language == "bash":
            proc = await asyncio.create_subprocess_shell(
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(err_b.decode("utf-8", errors="replace").strip())
            return out_b.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Unsupported macOS script language '{language}'.")

    # ------------------------------------------------------------------
    # Tier 2: ApplicationServices AXUIElement Accessibility Tree
    # ------------------------------------------------------------------

    async def get_accessibility_tree(
        self,
        app_or_window: str | None = None,
        max_depth: int = 4,
    ) -> list[AccessibilityNode]:
        return await asyncio.to_thread(self._walk_ax_tree_sync, app_or_window, max_depth)

    def _walk_ax_tree_sync(
        self,
        app_filter: str | None,
        max_depth: int,
    ) -> list[AccessibilityNode]:
        try:
            import ApplicationServices
            import Quartz
        except ImportError:
            return []

        self._ax_refs.clear()
        self._ax_bounds.clear()
        nodes: list[AccessibilityNode] = []
        needle = (app_filter or "").strip().lower()

        def attr_val(elem: Any, attr_name: str) -> Any:
            err, val = ApplicationServices.AXUIElementCopyAttributeValue(elem, attr_name, None)
            return val if err == 0 else None

        def walk(elem: Any, node_id: str, depth: int) -> AccessibilityNode | None:
            if elem is None or depth > max_depth:
                return None
            role = str(attr_val(elem, "AXRole") or "AXElement")
            title = str(attr_val(elem, "AXTitle") or attr_val(elem, "AXDescription") or "")
            val = str(attr_val(elem, "AXValue") or "")[:140]

            rect: Rect | None = None
            pos_ref = attr_val(elem, "AXPosition")
            size_ref = attr_val(elem, "AXSize")
            if pos_ref is not None and size_ref is not None:
                ok_p, pt = ApplicationServices.AXValueGetValue(
                    pos_ref, ApplicationServices.kAXValueCGPointType, None
                )
                ok_s, sz = ApplicationServices.AXValueGetValue(
                    size_ref, ApplicationServices.kAXValueCGSizeType, None
                )
                if ok_p and ok_s and pt is not None and sz is not None:
                    rect = Rect(int(pt.x), int(pt.y), int(sz.width), int(sz.height))

            err_a, act_names = ApplicationServices.AXUIElementCopyActionNames(elem, None)
            actions = [str(a) for a in act_names] if err_a == 0 and act_names else []

            self._ax_refs[node_id] = elem
            if rect and rect.is_visible():
                self._ax_bounds[node_id] = rect

            children: list[AccessibilityNode] = []
            if depth < max_depth:
                raw_children = attr_val(elem, "AXChildren") or []
                for idx, ch in enumerate(list(raw_children)[:35]):
                    c_node = walk(ch, f"{node_id}.{idx}", depth + 1)
                    if c_node is not None:
                        children.append(c_node)

            if not title and not val and not actions and not children:
                return None

            return AccessibilityNode(
                node_id=node_id,
                role=role,
                name=title,
                value=val,
                bounds=rect,
                actions=actions,
                children=children,
            )

        win_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        seen_pids: set[int] = set()
        for w in win_list or []:
            owner = str(w.get("kCGWindowOwnerName", ""))
            pid = int(w.get("kCGWindowOwnerPID", 0))
            if pid <= 0 or pid in seen_pids:
                continue
            if needle and needle not in owner.lower():
                continue
            seen_pids.add(pid)
            app_ref = ApplicationServices.AXUIElementCreateApplication(pid)
            root_node = walk(app_ref, f"ax:{pid}", 0)
            if root_node is not None:
                nodes.append(root_node)
            if len(nodes) >= 6:
                break

        return nodes

    async def invoke_accessibility_element(
        self,
        node_id: str,
        action: str = "AXPress",
        value: str | None = None,
    ) -> bool:
        elem = self._ax_refs.get(node_id)
        if elem is not None:
            try:
                import ApplicationServices

                if value is not None:
                    err = ApplicationServices.AXUIElementSetAttributeValue(elem, "AXValue", value)
                    if err == 0:
                        return True
                act_name = "AXPress" if action.lower() in ("click", "press", "axpress") else action
                err = ApplicationServices.AXUIElementPerformAction(elem, act_name)
                if err == 0:
                    return True
            except Exception as exc:
                logger.debug("macos_ax_invoke_error", error=str(exc))

        rect = self._ax_bounds.get(node_id)
        if rect and rect.is_visible():
            cx, cy = rect.center
            await self.click(cx, cy)
            if value is not None:
                await self.type_text(value)
            return True
        return False

    # ------------------------------------------------------------------
    # Tier 3: Quartz CGEvent Input Synthesis
    # ------------------------------------------------------------------

    async def move_mouse(self, x: int, y: int, smooth: bool = True) -> None:
        pts = cubic_bezier_points(self._cursor_pos, (x, y), steps=10 if smooth else 1)

        def _move() -> None:
            import time

            import Quartz

            for px, py in pts:
                ev = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved, (float(px), float(py)), Quartz.kCGMouseButtonLeft
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                if smooth:
                    time.sleep(0.008)

        await asyncio.to_thread(_move)
        self._cursor_pos = (x, y)

    async def click(
        self,
        x: int,
        y: int,
        button: MouseButton = "left",
        count: int = 1,
    ) -> None:
        await self.move_mouse(x, y, smooth=True)

        def _click() -> None:
            import Quartz

            btn_map = {
                "left": (
                    Quartz.kCGEventLeftMouseDown,
                    Quartz.kCGEventLeftMouseUp,
                    Quartz.kCGMouseButtonLeft,
                ),
                "right": (
                    Quartz.kCGEventRightMouseDown,
                    Quartz.kCGEventRightMouseUp,
                    Quartz.kCGMouseButtonRight,
                ),
                "middle": (
                    Quartz.kCGEventOtherMouseDown,
                    Quartz.kCGEventOtherMouseUp,
                    Quartz.kCGMouseButtonCenter,
                ),
            }
            down_t, up_t, btn = btn_map[button]
            for click_num in range(1, max(1, count) + 1):
                ev_down = Quartz.CGEventCreateMouseEvent(None, down_t, (float(x), float(y)), btn)
                Quartz.CGEventSetIntegerValueField(
                    ev_down, Quartz.kCGMouseEventClickState, click_num
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_down)

                ev_up = Quartz.CGEventCreateMouseEvent(None, up_t, (float(x), float(y)), btn)
                Quartz.CGEventSetIntegerValueField(ev_up, Quartz.kCGMouseEventClickState, click_num)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)

        await asyncio.to_thread(_click)

    async def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: MouseButton = "left",
    ) -> None:
        await self.move_mouse(start_x, start_y, smooth=True)
        pts = cubic_bezier_points((start_x, start_y), (end_x, end_y), steps=12)

        def _drag() -> None:
            import time

            import Quartz

            ev_down = Quartz.CGEventCreateMouseEvent(
                None,
                Quartz.kCGEventLeftMouseDown,
                (float(start_x), float(start_y)),
                Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_down)
            for px, py in pts:
                ev_drag = Quartz.CGEventCreateMouseEvent(
                    None,
                    Quartz.kCGEventLeftMouseDragged,
                    (float(px), float(py)),
                    Quartz.kCGMouseButtonLeft,
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_drag)
                time.sleep(0.01)
            ev_up = Quartz.CGEventCreateMouseEvent(
                None,
                Quartz.kCGEventLeftMouseUp,
                (float(end_x), float(end_y)),
                Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)

        await asyncio.to_thread(_drag)
        self._cursor_pos = (end_x, end_y)

    async def scroll(self, dx: int, dy: int, x: int | None = None, y: int | None = None) -> None:
        if x is not None and y is not None:
            await self.move_mouse(x, y, smooth=False)

        def _scroll() -> None:
            import Quartz

            ev = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitLine, 2, int(dy), int(dx)
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

        await asyncio.to_thread(_scroll)

    async def press_hotkey(self, keys: list[str]) -> None:
        def _hotkey() -> None:
            from pynput.keyboard import Controller as KeyController
            from pynput.keyboard import Key

            kb = KeyController()
            items: list[Any] = [getattr(Key, k.lower(), k.lower()) for k in keys]
            for it in items:
                kb.press(it)
            for it in reversed(items):
                kb.release(it)

        await asyncio.to_thread(_hotkey)

    async def type_text(self, text: str) -> None:
        def _type() -> None:
            import Quartz

            for ch in text:
                ev_down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
                Quartz.CGEventKeyboardSetUnicodeString(ev_down, len(ch), ch)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_down)

                ev_up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
                Quartz.CGEventKeyboardSetUnicodeString(ev_up, len(ch), ch)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)

        await asyncio.to_thread(_type)

    async def check_permissions(self) -> PermissionReport:
        a11y_ok = False
        screen_ok = False
        try:
            import ApplicationServices
            import Quartz

            a11y_ok = bool(ApplicationServices.AXIsProcessTrusted())
            screen_ok = bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            pass

        return PermissionReport(
            microphone=True,
            accessibility=a11y_ok,
            screen_recording=screen_ok,
            input_synthesis=a11y_ok,
            details={"platform": "darwin", "process_count": str(len(psutil.pids()))},
        )
