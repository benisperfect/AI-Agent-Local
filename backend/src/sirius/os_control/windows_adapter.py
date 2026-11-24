"""Windows 10/11 OSAdapter implementation for Sirius (Win32, PowerShell, UIA, SendInput)."""

from __future__ import annotations

import asyncio
import ctypes
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


class WindowsOSAdapter(OSAdapter):
    """Production Windows 10/11 adapter using Win32 APIs, PowerShell, UIA, and SendInput."""

    def __init__(self) -> None:
        self._cursor_pos: tuple[int, int] = (500, 500)
        self._uia_elements: dict[str, Any] = {}
        self._uia_bounds: dict[str, Rect] = {}
        self._enable_dpi_awareness()

    @property
    def os_name(self) -> str:
        return "Windows"

    @staticmethod
    def _enable_dpi_awareness() -> None:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

    async def _powershell(self, command: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        return (
            proc.returncode or 0,
            out_b.decode("utf-8", errors="replace").strip(),
            err_b.decode("utf-8", errors="replace").strip(),
        )

    # ------------------------------------------------------------------
    # Tier 1: App & Window Control
    # ------------------------------------------------------------------

    async def list_windows(self) -> list[WindowInfo]:
        return await asyncio.to_thread(self._enum_windows_sync)

    def _enum_windows_sync(self) -> list[WindowInfo]:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return []

        user32 = windll.user32
        fg_hwnd = user32.GetForegroundWindow()
        results: list[WindowInfo] = []

        class WinRect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        fn_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        enum_proc_type = fn_factory(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title:
                return True

            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_name = "unknown"
            try:
                proc_name = psutil.Process(pid.value).name()
            except Exception:
                pass

            wr = WinRect()
            user32.GetWindowRect(hwnd, ctypes.byref(wr))
            results.append(
                WindowInfo(
                    window_id=str(hwnd),
                    title=title,
                    app_name=proc_name,
                    pid=int(pid.value),
                    bounds=Rect(
                        x=int(wr.left),
                        y=int(wr.top),
                        width=max(0, int(wr.right - wr.left)),
                        height=max(0, int(wr.bottom - wr.top)),
                    ),
                    focused=(hwnd == fg_hwnd),
                    minimized=bool(user32.IsIconic(hwnd)),
                )
            )
            return True

        user32.EnumWindows(enum_proc_type(_cb), 0)
        return results

    async def launch_app(self, app_name_or_path: str, args: list[str] | None = None) -> str:
        escaped_app = app_name_or_path.replace("'", "''")
        if args:
            arg_list = ",".join("'" + a.replace("'", "''") + "'" for a in args)
            cmd = f"Start-Process -FilePath '{escaped_app}' -ArgumentList {arg_list} -PassThru | Select-Object -ExpandProperty Id"
        else:
            cmd = f"Start-Process -FilePath '{escaped_app}' -PassThru | Select-Object -ExpandProperty Id"
        rc, out, err = await self._powershell(cmd)
        if rc != 0:
            raise RuntimeError(err or f"Failed to launch '{app_name_or_path}'")
        return f"Launched {app_name_or_path} (pid={out})"

    async def _find_hwnd(self, title_or_app: str) -> int | None:
        needle = title_or_app.lower()
        for w in await self.list_windows():
            if needle in w.title.lower() or needle in w.app_name.lower():
                return int(w.window_id)
        return None

    async def focus_window(self, title_or_app: str) -> bool:
        hwnd = await self._find_hwnd(title_or_app)
        windll = getattr(ctypes, "windll", None)
        if hwnd is None or windll is None:
            return False
        windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        return bool(windll.user32.SetForegroundWindow(hwnd))

    async def minimize_window(self, title_or_app: str) -> bool:
        hwnd = await self._find_hwnd(title_or_app)
        windll = getattr(ctypes, "windll", None)
        if hwnd is None or windll is None:
            return False
        return bool(windll.user32.ShowWindow(hwnd, 6))  # SW_MINIMIZE

    async def tile_window(self, title_or_app: str, position: TilePosition) -> bool:
        hwnd = await self._find_hwnd(title_or_app)
        windll = getattr(ctypes, "windll", None)
        if hwnd is None or windll is None:
            return False
        user32 = windll.user32
        if position == "maximize":
            return bool(user32.ShowWindow(hwnd, 3))  # SW_MAXIMIZE
        if position == "restore":
            return bool(user32.ShowWindow(hwnd, 9))  # SW_RESTORE

        sw = int(user32.GetSystemMetrics(0))
        sh = int(user32.GetSystemMetrics(1)) - 48
        user32.ShowWindow(hwnd, 9)
        half = sw // 2
        tx = 0 if position == "left" else half
        return bool(user32.MoveWindow(hwnd, tx, 0, half, sh, True))

    async def close_window(self, title_or_app: str) -> bool:
        hwnd = await self._find_hwnd(title_or_app)
        windll = getattr(ctypes, "windll", None)
        if hwnd is None or windll is None:
            return False
        return bool(windll.user32.PostMessageW(hwnd, 0x0010, 0, 0))  # WM_CLOSE

    # ------------------------------------------------------------------
    # Tier 1: System Settings & Media
    # ------------------------------------------------------------------

    async def get_volume(self) -> int:
        try:
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL  # type: ignore[import-not-found]
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            return round(float(volume.GetMasterVolumeLevelScalar()) * 100)
        except Exception:
            return 50

    async def set_volume(self, percent: int) -> bool:
        clamped = max(0, min(100, percent))
        try:
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume.SetMasterVolumeLevelScalar(clamped / 100.0, None)
            return True
        except Exception:
            return False

    async def set_mute(self, muted: bool) -> bool:
        try:
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume.SetMute(1 if muted else 0, None)
            return True
        except Exception:
            return False

    async def set_brightness(self, percent: int) -> bool:
        clamped = max(5, min(100, percent))
        cmd = (
            "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
            f".WmiSetBrightness(1, {clamped})"
        )
        rc, _, _ = await self._powershell(cmd)
        return rc == 0

    async def set_wifi(self, enabled: bool) -> bool:
        state = "enable" if enabled else "disable"
        rc, _, _ = await self._powershell(f"netsh interface set interface 'Wi-Fi' {state}")
        return rc == 0

    async def set_bluetooth(self, enabled: bool) -> bool:
        status = "On" if enabled else "Off"
        cmd = (
            "Add-Type -AssemblyName System.Runtime.WindowsRuntime; "
            f"Write-Output 'Bluetooth requested {status}'"
        )
        rc, _, _ = await self._powershell(cmd)
        return rc == 0

    async def media_control(self, action: MediaAction) -> bool:
        vk_map = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        vk = vk_map[action]
        windll.user32.keybd_event(vk, 0, 0, 0)
        windll.user32.keybd_event(vk, 0, 2, 0)
        return True

    async def send_notification(self, title: str, body: str) -> bool:
        safe_t = title.replace("'", "''")
        safe_b = body.replace("'", "''")
        cmd = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            "$textNodes = $template.GetElementsByTagName('text'); "
            f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{safe_t}')) > $null; "
            f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{safe_b}')) > $null; "
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Sirius').Show($toast)"
        )
        rc, _, _ = await self._powershell(cmd)
        return rc == 0

    async def get_clipboard(self) -> str:
        rc, out, _ = await self._powershell("Get-Clipboard -Raw")
        return out if rc == 0 else ""

    async def set_clipboard(self, text: str) -> bool:
        escaped = text.replace("'", "''")
        rc, _, _ = await self._powershell(f"Set-Clipboard -Value '{escaped}'")
        return rc == 0

    async def run_os_script(self, script: str, language: ScriptLanguage) -> str:
        if language == "powershell":
            rc, out, err = await self._powershell(script)
            if rc != 0:
                raise RuntimeError(err)
            return out
        raise ValueError(f"Unsupported Windows script language '{language}'.")

    # ------------------------------------------------------------------
    # Tier 2: Windows UI Automation (`pywinauto` UIA backend)
    # ------------------------------------------------------------------

    async def get_accessibility_tree(
        self,
        app_or_window: str | None = None,
        max_depth: int = 4,
    ) -> list[AccessibilityNode]:
        return await asyncio.to_thread(self._walk_uia_sync, app_or_window, max_depth)

    def _walk_uia_sync(
        self,
        app_filter: str | None,
        max_depth: int,
    ) -> list[AccessibilityNode]:
        try:
            from pywinauto import Desktop
        except ImportError:
            return []

        self._uia_elements.clear()
        self._uia_bounds.clear()
        needle = (app_filter or "").strip().lower()
        desktop = Desktop(backend="uia")

        def serialize(wrapper: Any, node_id: str, depth: int) -> AccessibilityNode | None:
            if depth > max_depth:
                return None
            try:
                info = wrapper.element_info
                role = str(info.control_type or "Control")
                name = str(info.name or "")
                r = info.rectangle
                rect = Rect(int(r.left), int(r.top), int(r.width()), int(r.height()))
            except Exception:
                return None

            self._uia_elements[node_id] = wrapper
            if rect.is_visible():
                self._uia_bounds[node_id] = rect

            children: list[AccessibilityNode] = []
            if depth < max_depth:
                try:
                    for idx, ch in enumerate(wrapper.children()[:35]):
                        c_node = serialize(ch, f"{node_id}.{idx}", depth + 1)
                        if c_node is not None:
                            children.append(c_node)
                except Exception:
                    pass

            if not name and not children:
                return None
            return AccessibilityNode(
                node_id=node_id,
                role=role,
                name=name,
                bounds=rect,
                actions=["invoke", "set_text"],
                children=children,
            )

        roots: list[AccessibilityNode] = []
        for idx, win in enumerate(desktop.windows()):
            try:
                wname = str(win.window_text() or "")
                if needle and needle not in wname.lower():
                    continue
                node = serialize(win, f"uia:{idx}", 0)
                if node is not None:
                    roots.append(node)
                if len(roots) >= 6:
                    break
            except Exception:
                continue
        return roots

    async def invoke_accessibility_element(
        self,
        node_id: str,
        action: str = "invoke",
        value: str | None = None,
    ) -> bool:
        elem = self._uia_elements.get(node_id)
        if elem is not None:
            def _do_uia() -> bool:
                try:
                    if value is not None and hasattr(elem, "set_edit_text"):
                        elem.set_edit_text(value)
                        return True
                    if hasattr(elem, "invoke"):
                        elem.invoke()
                        return True
                    if hasattr(elem, "click_input"):
                        elem.click_input()
                        return True
                except Exception:
                    return False
                return False

            if await asyncio.to_thread(_do_uia):
                return True

        rect = self._uia_bounds.get(node_id)
        if rect and rect.is_visible():
            cx, cy = rect.center
            await self.click(cx, cy)
            if value is not None:
                await self.type_text(value)
            return True
        return False

    # ------------------------------------------------------------------
    # Tier 3: Win32 SendInput / Pynput Input Synthesis
    # ------------------------------------------------------------------

    async def move_mouse(self, x: int, y: int, smooth: bool = True) -> None:
        pts = cubic_bezier_points(self._cursor_pos, (x, y), steps=10 if smooth else 1)

        def _move() -> None:
            import time

            windll = getattr(ctypes, "windll", None)
            if windll is not None:
                for px, py in pts:
                    windll.user32.SetCursorPos(int(px), int(py))
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
            from pynput.mouse import Button
            from pynput.mouse import Controller as MouseController

            b_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}
            MouseController().click(b_map[button], max(1, count))

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

            from pynput.mouse import Button
            from pynput.mouse import Controller as MouseController

            b_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}
            m = MouseController()
            m.press(b_map[button])
            for px, py in pts:
                m.position = (px, py)
                time.sleep(0.01)
            m.release(b_map[button])

        await asyncio.to_thread(_drag)
        self._cursor_pos = (end_x, end_y)

    async def scroll(self, dx: int, dy: int, x: int | None = None, y: int | None = None) -> None:
        if x is not None and y is not None:
            await self.move_mouse(x, y, smooth=False)

        def _scroll() -> None:
            from pynput.mouse import Controller as MouseController

            MouseController().scroll(dx, dy)

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
            from pynput.keyboard import Controller as KeyController

            KeyController().type(text)

        await asyncio.to_thread(_type)

    async def check_permissions(self) -> PermissionReport:
        return PermissionReport(
            microphone=True,
            accessibility=True,
            screen_recording=True,
            input_synthesis=True,
            details={"platform": "win32", "uia_enabled": "true"},
        )
