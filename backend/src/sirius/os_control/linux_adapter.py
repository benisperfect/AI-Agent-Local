"""Linux (X11 & Wayland) OSAdapter implementation for Sirius."""

from __future__ import annotations

import asyncio
import configparser
import json
import os
import re
import shutil
from pathlib import Path
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

_ATSPI_DUMP_SCRIPT = r"""
import json, sys
import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi

target_filter = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
max_depth = int(sys.argv[2]) if len(sys.argv) > 2 else 4

Atspi.init()
desktop = Atspi.get_desktop(0)

def serialize_node(acc, path_prefix, depth):
    if acc is None or depth > max_depth:
        return None
    try:
        role = acc.get_role_name() or "unknown"
        name = acc.get_name() or ""
        desc = acc.get_description() or ""
    except Exception:
        return None

    bounds = None
    try:
        comp = acc.get_component_iface()
        if comp is not None:
            ext = comp.get_extents(Atspi.CoordType.SCREEN)
            bounds = {"x": int(ext.x), "y": int(ext.y), "width": int(ext.width), "height": int(ext.height)}
    except Exception:
        bounds = None

    actions = []
    try:
        act_iface = acc.get_action_iface()
        if act_iface is not None:
            n_act = act_iface.get_n_actions()
            for i in range(min(n_act, 6)):
                aname = act_iface.get_action_name(i)
                if aname:
                    actions.append(aname)
    except Exception:
        pass

    val_str = ""
    try:
        txt_iface = acc.get_text_iface()
        if txt_iface is not None:
            cnt = txt_iface.get_character_count()
            if cnt > 0:
                val_str = txt_iface.get_text(0, min(cnt, 160)) or ""
    except Exception:
        pass

    children = []
    if depth < max_depth:
        try:
            c_count = min(acc.get_child_count(), 35)
            for idx in range(c_count):
                child = acc.get_child_at_index(idx)
                c_node = serialize_node(child, f"{path_prefix}.{idx}", depth + 1)
                if c_node is not None:
                    children.append(c_node)
        except Exception:
            pass

    if not name and not actions and not val_str and not children:
        return None

    return {
        "node_id": path_prefix,
        "role": role,
        "name": name,
        "value": val_str,
        "description": desc,
        "bounds": bounds,
        "actions": actions,
        "enabled": True,
        "focused": False,
        "children": children,
    }

roots = []
if desktop is not None:
    app_count = desktop.get_child_count()
    for app_idx in range(app_count):
        try:
            app = desktop.get_child_at_index(app_idx)
            if app is None:
                continue
            app_name = (app.get_name() or "").strip()
            if target_filter and target_filter not in app_name.lower():
                continue
            serialized = serialize_node(app, f"atspi:{app_idx}", 0)
            if serialized is not None:
                roots.append(serialized)
                if len(roots) >= 8:
                    break
        except Exception:
            continue

print(json.dumps(roots))
"""

_ATSPI_INVOKE_SCRIPT = r"""
import json, sys
import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi

node_id = sys.argv[1]
action_req = sys.argv[2].lower()
value_arg = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "__NONE__" else None

if not node_id.startswith("atspi:"):
    print(json.dumps({"ok": False, "error": "Invalid node_id prefix"}))
    sys.exit(0)

indices = [int(part) for part in node_id.removeprefix("atspi:").split(".") if part.isdigit()]
Atspi.init()
cur = Atspi.get_desktop(0)
for idx in indices:
    if cur is None or idx >= cur.get_child_count():
        cur = None
        break
    cur = cur.get_child_at_index(idx)

if cur is None:
    print(json.dumps({"ok": False, "error": "Element no longer exists"}))
    sys.exit(0)

if value_arg is not None:
    try:
        edit_iface = cur.get_editable_text_iface()
        if edit_iface is not None:
            edit_iface.set_text_contents(value_arg)
            print(json.dumps({"ok": True}))
            sys.exit(0)
    except Exception:
        pass

try:
    act_iface = cur.get_action_iface()
    if act_iface is not None and act_iface.get_n_actions() > 0:
        chosen = 0
        for i in range(act_iface.get_n_actions()):
            nm = (act_iface.get_action_name(i) or "").lower()
            if action_req in nm or nm in ("click", "press", "activate", "jump"):
                chosen = i
                break
        ok = bool(act_iface.do_action(chosen))
        print(json.dumps({"ok": ok}))
        sys.exit(0)
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
    sys.exit(0)

print(json.dumps({"ok": False, "error": "No matching AT-SPI action interface"}))
"""


class LinuxOSAdapter(OSAdapter):
    """Production Linux adapter supporting both Wayland and X11 desktop environments."""

    def __init__(self) -> None:
        self._session_type = os.environ.get("XDG_SESSION_TYPE", "x11").lower()
        self._clipboard_fallback: str = ""
        self._cursor_pos: tuple[int, int] = (400, 400)
        self._node_bounds_cache: dict[str, Rect] = {}
        self._sys_python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"

    @property
    def os_name(self) -> str:
        return f"Linux ({self._session_type})"

    async def _exec(
        self,
        *args: str,
        stdin_bytes: bytes | None = None,
        timeout_seconds: float = 5.0,
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(stdin_bytes), timeout=timeout_seconds
            )
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return (124, "", "Command timed out")
        return (
            proc.returncode or 0,
            out_b.decode("utf-8", errors="replace").strip(),
            err_b.decode("utf-8", errors="replace").strip(),
        )

    # ------------------------------------------------------------------
    # Tier 1: App & Window Control
    # ------------------------------------------------------------------

    async def list_windows(self) -> list[WindowInfo]:
        windows: list[WindowInfo] = []
        if shutil.which("wmctrl"):
            rc, out, _ = await self._exec("wmctrl", "-lGpx")
            if rc == 0 and out:
                for line in out.splitlines():
                    parts = line.split(None, 8)
                    if len(parts) < 9:
                        continue
                    win_id, _desk, pid_s, x_s, y_s, w_s, h_s, wm_class, title = parts
                    windows.append(
                        WindowInfo(
                            window_id=win_id,
                            title=title,
                            app_name=wm_class.split(".")[-1],
                            pid=int(pid_s) if pid_s.isdigit() else 0,
                            bounds=Rect(
                                x=int(x_s),
                                y=int(y_s),
                                width=max(0, int(w_s)),
                                height=max(0, int(h_s)),
                            ),
                        )
                    )
        if windows:
            return windows

        # Pure Wayland fallback: enumerate desktop GUI user processes
        uid = os.getuid()
        for proc in psutil.process_iter(["pid", "name", "uids", "cmdline"]):
            try:
                uids = proc.info.get("uids")
                if uids and uids.real != uid:
                    continue
                name = str(proc.info.get("name") or "")
                if name in {"brave", "chrome", "firefox", "code", "zed", "gnome-terminal-server", "nautilus"}:
                    windows.append(
                        WindowInfo(
                            window_id=f"pid:{proc.info['pid']}",
                            title=name,
                            app_name=name,
                            pid=int(proc.info["pid"]),
                            bounds=Rect(0, 0, 1280, 800),
                        )
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return windows

    async def launch_app(self, app_name_or_path: str, args: list[str] | None = None) -> str:
        extra = args or []
        direct = shutil.which(app_name_or_path)
        if direct:
            proc = await asyncio.create_subprocess_exec(
                direct,
                *extra,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            return f"Launched {direct} (pid={proc.pid})"

        desktop_id = self._find_desktop_entry(app_name_or_path)
        if desktop_id and shutil.which("gtk-launch"):
            proc = await asyncio.create_subprocess_exec(
                "gtk-launch",
                desktop_id,
                *extra,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            return f"Launched desktop entry {desktop_id} (pid={proc.pid})"

        if shutil.which("xdg-open"):
            proc = await asyncio.create_subprocess_exec(
                "xdg-open",
                app_name_or_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            return f"Opened {app_name_or_path} via xdg-open (pid={proc.pid})"

        raise FileNotFoundError(f"Could not find Linux application or binary '{app_name_or_path}'.")

    @staticmethod
    def _find_desktop_entry(query: str) -> str | None:
        needle = query.strip().lower()
        search_dirs = [
            Path.home() / ".local/share/applications",
            Path("/usr/share/applications"),
            Path("/var/lib/flatpak/exports/share/applications"),
            Path("/var/lib/snapd/desktop/applications"),
        ]
        for folder in search_dirs:
            if not folder.exists():
                continue
            for entry in folder.glob("*.desktop"):
                if needle in entry.stem.lower():
                    return entry.name
                cp = configparser.ConfigParser(interpolation=None)
                try:
                    cp.read(entry, encoding="utf-8")
                    disp = cp.get("Desktop Entry", "Name", fallback="").lower()
                    if needle in disp:
                        return entry.name
                except Exception:
                    continue
        return None

    async def focus_window(self, title_or_app: str) -> bool:
        if shutil.which("wmctrl"):
            rc, _, _ = await self._exec("wmctrl", "-a", title_or_app)
            if rc == 0:
                return True
        if shutil.which("xdotool"):
            rc, out, _ = await self._exec("xdotool", "search", "--name", title_or_app)
            if rc == 0 and out:
                wid = out.splitlines()[0].strip()
                rc2, _, _ = await self._exec("xdotool", "windowactivate", wid)
                return rc2 == 0
        return False

    async def minimize_window(self, title_or_app: str) -> bool:
        if shutil.which("xdotool"):
            rc, out, _ = await self._exec("xdotool", "search", "--name", title_or_app)
            if rc == 0 and out:
                wid = out.splitlines()[0].strip()
                rc2, _, _ = await self._exec("xdotool", "windowminimize", wid)
                return rc2 == 0
        if shutil.which("wmctrl"):
            rc, _, _ = await self._exec("wmctrl", "-r", title_or_app, "-b", "add,hidden")
            return rc == 0
        return False

    async def tile_window(self, title_or_app: str, position: TilePosition) -> bool:
        if not shutil.which("wmctrl"):
            return False
        if position == "maximize":
            rc, _, _ = await self._exec(
                "wmctrl", "-r", title_or_app, "-b", "add,maximized_vert,maximized_horz"
            )
            return rc == 0
        if position == "restore":
            rc, _, _ = await self._exec(
                "wmctrl", "-r", title_or_app, "-b", "remove,maximized_vert,maximized_horz"
            )
            return rc == 0

        # Query primary monitor dimensions via xrandr or default 1920x1080
        screen_w, screen_h = await self._primary_screen_size()
        await self._exec(
            "wmctrl", "-r", title_or_app, "-b", "remove,maximized_vert,maximized_horz"
        )
        half_w = screen_w // 2
        target_x = 0 if position == "left" else half_w
        rc, _, _ = await self._exec(
            "wmctrl", "-r", title_or_app, "-e", f"0,{target_x},0,{half_w},{screen_h}"
        )
        return rc == 0

    async def _primary_screen_size(self) -> tuple[int, int]:
        if shutil.which("xrandr"):
            rc, out, _ = await self._exec("xrandr", "--current")
            if rc == 0:
                m = re.search(r"current\s+(\d+)\s+x\s+(\d+)", out)
                if m:
                    return int(m.group(1)), int(m.group(2))
        return (1920, 1080)

    async def close_window(self, title_or_app: str) -> bool:
        if shutil.which("wmctrl"):
            rc, _, _ = await self._exec("wmctrl", "-c", title_or_app)
            if rc == 0:
                return True
        needle = title_or_app.lower()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if needle in str(proc.info.get("name") or "").lower():
                    proc.terminate()
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    # ------------------------------------------------------------------
    # Tier 1: System Settings & Media
    # ------------------------------------------------------------------

    async def get_volume(self) -> int:
        if shutil.which("wpctl"):
            rc, out, _ = await self._exec("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
            if rc == 0:
                m = re.search(r"([0-9]+\.[0-9]+)", out)
                if m:
                    return round(float(m.group(1)) * 100)
        if shutil.which("pactl"):
            rc, out, _ = await self._exec("pactl", "get-sink-volume", "@DEFAULT_SINK@")
            if rc == 0:
                m = re.search(r"(\d+)%", out)
                if m:
                    return int(m.group(1))
        if shutil.which("amixer"):
            rc, out, _ = await self._exec("amixer", "get", "Master")
            if rc == 0:
                m = re.search(r"\[(\d+)%\]", out)
                if m:
                    return int(m.group(1))
        return 50

    async def set_volume(self, percent: int) -> bool:
        clamped = max(0, min(100, percent))
        if shutil.which("wpctl"):
            rc, _, _ = await self._exec(
                "wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{clamped}%"
            )
            return rc == 0
        if shutil.which("pactl"):
            rc, _, _ = await self._exec(
                "pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{clamped}%"
            )
            return rc == 0
        if shutil.which("amixer"):
            rc, _, _ = await self._exec("amixer", "set", "Master", f"{clamped}%")
            return rc == 0
        return False

    async def set_mute(self, muted: bool) -> bool:
        flag = "1" if muted else "0"
        if shutil.which("wpctl"):
            rc, _, _ = await self._exec("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", flag)
            return rc == 0
        if shutil.which("pactl"):
            rc, _, _ = await self._exec("pactl", "set-sink-mute", "@DEFAULT_SINK@", flag)
            return rc == 0
        return False

    async def set_brightness(self, percent: int) -> bool:
        clamped = max(5, min(100, percent))
        if shutil.which("brightnessctl"):
            rc, _, _ = await self._exec("brightnessctl", "set", f"{clamped}%")
            if rc == 0:
                return True
        if shutil.which("gdbus"):
            rc, _, _ = await self._exec(
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.SettingsDaemon.Power",
                "--object-path",
                "/org/gnome/SettingsDaemon/Power",
                "--method",
                "org.freedesktop.DBus.Properties.Set",
                "org.gnome.SettingsDaemon.Power.Screen",
                "Brightness",
                f"<int32 {clamped}>",
            )
            if rc == 0:
                return True
        return False

    async def set_wifi(self, enabled: bool) -> bool:
        if shutil.which("nmcli"):
            rc, _, _ = await self._exec("nmcli", "radio", "wifi", "on" if enabled else "off")
            return rc == 0
        if shutil.which("rfkill"):
            rc, _, _ = await self._exec("rfkill", "unblock" if enabled else "block", "wifi")
            return rc == 0
        return False

    async def set_bluetooth(self, enabled: bool) -> bool:
        if shutil.which("bluetoothctl"):
            rc, _, _ = await self._exec("bluetoothctl", "power", "on" if enabled else "off")
            if rc == 0:
                return True
        if shutil.which("rfkill"):
            rc, _, _ = await self._exec("rfkill", "unblock" if enabled else "block", "bluetooth")
            return rc == 0
        return False

    async def media_control(self, action: MediaAction) -> bool:
        if shutil.which("playerctl"):
            mapping = {
                "play_pause": "play-pause",
                "next": "next",
                "previous": "previous",
                "stop": "stop",
            }
            rc, _, _ = await self._exec("playerctl", mapping[action])
            if rc == 0:
                return True
        key_map = {
            "play_pause": ["XF86AudioPlay"],
            "next": ["XF86AudioNext"],
            "previous": ["XF86AudioPrev"],
            "stop": ["XF86AudioStop"],
        }
        await self.press_hotkey(key_map[action])
        return True

    async def send_notification(self, title: str, body: str) -> bool:
        if shutil.which("notify-send"):
            rc, _, _ = await self._exec("notify-send", "--app-name=Sirius", title, body)
            return rc == 0
        return False

    # ------------------------------------------------------------------
    # Tier 1: Clipboard & OS Scripting
    # ------------------------------------------------------------------

    async def get_clipboard(self) -> str:
        if self._session_type == "wayland" and shutil.which("wl-paste"):
            rc, out, _ = await self._exec("wl-paste", "--no-newline")
            if rc == 0:
                return out
        if shutil.which("xclip"):
            rc, out, _ = await self._exec("xclip", "-selection", "clipboard", "-o")
            if rc == 0:
                return out
        if shutil.which("xsel"):
            rc, out, _ = await self._exec("xsel", "--clipboard", "--output")
            if rc == 0:
                return out
        return self._clipboard_fallback

    async def set_clipboard(self, text: str) -> bool:
        self._clipboard_fallback = text
        data = text.encode("utf-8")
        candidates: list[list[str]] = []
        if self._session_type == "wayland" and shutil.which("wl-copy"):
            candidates.append(["wl-copy"])
        if shutil.which("xclip"):
            candidates.append(["xclip", "-selection", "clipboard"])
        if shutil.which("xsel"):
            candidates.append(["xsel", "--clipboard", "--input"])

        for cmd in candidates:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
                if proc.stdin is not None:
                    proc.stdin.write(data)
                    await proc.stdin.drain()
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=1.0)
                if proc.returncode == 0:
                    return True
            except TimeoutError:
                # Background clipboard selection daemon spawned cleanly
                return True
            except Exception:
                continue
        return True

    async def run_os_script(self, script: str, language: ScriptLanguage) -> str:
        if language == "dbus":
            if not shutil.which("gdbus"):
                raise RuntimeError("gdbus is not installed on this Linux system.")
            parts = script.split()
            rc, out, err = await self._exec("gdbus", *parts)
            if rc != 0:
                raise RuntimeError(err or f"gdbus exited with status {rc}")
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
        raise ValueError(f"Script language '{language}' is not supported on Linux.")

    # ------------------------------------------------------------------
    # Tier 2: AT-SPI2 Semantic Accessibility Tree
    # ------------------------------------------------------------------

    async def get_accessibility_tree(
        self,
        app_or_window: str | None = None,
        max_depth: int = 4,
    ) -> list[AccessibilityNode]:
        rc, out, err = await self._exec(
            self._sys_python,
            "-c",
            _ATSPI_DUMP_SCRIPT,
            app_or_window or "",
            str(max_depth),
        )
        if rc != 0 or not out:
            logger.debug("atspi_tree_empty_or_unavailable", rc=rc, err=err)
            return []

        try:
            raw_list = json.loads(out)
        except json.JSONDecodeError:
            return []

        self._node_bounds_cache.clear()
        nodes: list[AccessibilityNode] = []
        for item in raw_list:
            parsed = self._parse_a11y_dict(item)
            if parsed is not None:
                nodes.append(parsed)
        return nodes

    def _parse_a11y_dict(self, data: dict[str, Any]) -> AccessibilityNode | None:
        if not isinstance(data, dict):
            return None
        b_raw = data.get("bounds")
        rect: Rect | None = None
        if isinstance(b_raw, dict):
            rect = Rect(
                x=int(b_raw.get("x", 0)),
                y=int(b_raw.get("y", 0)),
                width=int(b_raw.get("width", 0)),
                height=int(b_raw.get("height", 0)),
            )
        node_id = str(data.get("node_id", ""))
        if rect and rect.is_visible() and node_id:
            self._node_bounds_cache[node_id] = rect

        children: list[AccessibilityNode] = []
        for ch in data.get("children", []):
            c_parsed = self._parse_a11y_dict(ch)
            if c_parsed is not None:
                children.append(c_parsed)

        return AccessibilityNode(
            node_id=node_id,
            role=str(data.get("role", "unknown")),
            name=str(data.get("name", "")),
            value=str(data.get("value", "")),
            description=str(data.get("description", "")),
            bounds=rect,
            actions=[str(a) for a in data.get("actions", [])],
            enabled=bool(data.get("enabled", True)),
            focused=bool(data.get("focused", False)),
            children=children,
        )

    async def invoke_accessibility_element(
        self,
        node_id: str,
        action: str = "click",
        value: str | None = None,
    ) -> bool:
        val_arg = value if value is not None else "__NONE__"
        rc, out, _ = await self._exec(
            self._sys_python,
            "-c",
            _ATSPI_INVOKE_SCRIPT,
            node_id,
            action,
            val_arg,
        )
        if rc == 0 and out:
            try:
                res = json.loads(out)
                if res.get("ok"):
                    return True
            except json.JSONDecodeError:
                pass

        # Coordinate click fallback if element exposed screen extents
        cached = self._node_bounds_cache.get(node_id)
        if cached and cached.is_visible():
            cx, cy = cached.center
            await self.click(cx, cy, button="left", count=1)
            if value is not None:
                await self.type_text(value)
            return True
        return False

    # ------------------------------------------------------------------
    # Tier 3: X11 + Wayland Smooth Input Synthesis
    # ------------------------------------------------------------------

    async def move_mouse(self, x: int, y: int, smooth: bool = True) -> None:
        points = cubic_bezier_points(self._cursor_pos, (x, y), steps=10 if smooth else 1)
        if self._session_type == "wayland" and shutil.which("ydotool"):
            for px, py in points:
                await self._exec("ydotool", "mousemove", "--absolute", "-x", str(px), "-y", str(py))
                if smooth:
                    await asyncio.sleep(0.008)
            self._cursor_pos = (x, y)
            return

        if shutil.which("xdotool"):
            for px, py in points:
                await self._exec("xdotool", "mousemove", str(px), str(py))
                if smooth:
                    await asyncio.sleep(0.008)
            self._cursor_pos = (x, y)
            return

        await asyncio.to_thread(self._pynput_move, points, smooth)
        self._cursor_pos = (x, y)

    @staticmethod
    def _pynput_move(points: list[tuple[int, int]], smooth: bool) -> None:
        import time

        from pynput.mouse import Controller as MouseController

        mouse = MouseController()
        for px, py in points:
            mouse.position = (px, py)
            if smooth:
                time.sleep(0.008)

    async def click(
        self,
        x: int,
        y: int,
        button: MouseButton = "left",
        count: int = 1,
    ) -> None:
        await self.move_mouse(x, y, smooth=True)
        if self._session_type == "wayland" and shutil.which("ydotool"):
            btn_code = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}[button]
            for _ in range(max(1, count)):
                await self._exec("ydotool", "click", btn_code)
            return

        if shutil.which("xdotool"):
            btn_num = {"left": "1", "middle": "2", "right": "3"}[button]
            await self._exec("xdotool", "click", "--repeat", str(max(1, count)), btn_num)
            return

        def _do_click() -> None:
            from pynput.mouse import Button
            from pynput.mouse import Controller as MouseController

            b_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}
            MouseController().click(b_map[button], max(1, count))

        await asyncio.to_thread(_do_click)

    async def drag(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        button: MouseButton = "left",
    ) -> None:
        await self.move_mouse(start_x, start_y, smooth=True)
        if shutil.which("xdotool"):
            btn_num = {"left": "1", "middle": "2", "right": "3"}[button]
            await self._exec("xdotool", "mousedown", btn_num)
            await self.move_mouse(end_x, end_y, smooth=True)
            await self._exec("xdotool", "mouseup", btn_num)
            return

        def _do_drag() -> None:
            import time

            from pynput.mouse import Button
            from pynput.mouse import Controller as MouseController

            b_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}
            m = MouseController()
            m.position = (start_x, start_y)
            m.press(b_map[button])
            for px, py in cubic_bezier_points((start_x, start_y), (end_x, end_y), steps=12):
                m.position = (px, py)
                time.sleep(0.01)
            m.release(b_map[button])

        await asyncio.to_thread(_do_drag)
        self._cursor_pos = (end_x, end_y)

    async def scroll(self, dx: int, dy: int, x: int | None = None, y: int | None = None) -> None:
        if x is not None and y is not None:
            await self.move_mouse(x, y, smooth=False)

        def _do_scroll() -> None:
            from pynput.mouse import Controller as MouseController

            MouseController().scroll(dx, dy)

        await asyncio.to_thread(_do_scroll)

    async def press_hotkey(self, keys: list[str]) -> None:
        if not keys:
            return
        if shutil.which("xdotool"):
            combo = "+".join(keys)
            rc, _, _ = await self._exec("xdotool", "key", combo)
            if rc == 0:
                return

        def _do_hotkey() -> None:
            from pynput.keyboard import Controller as KeyController
            from pynput.keyboard import Key

            kb = KeyController()
            resolved: list[Any] = []
            for k in keys:
                low = k.lower()
                attr = getattr(Key, low, None)
                resolved.append(attr if attr is not None else low)
            for item in resolved:
                kb.press(item)
            for item in reversed(resolved):
                kb.release(item)

        await asyncio.to_thread(_do_hotkey)

    async def type_text(self, text: str) -> None:
        if not text:
            return
        if self._session_type == "wayland" and shutil.which("wtype"):
            rc, _, _ = await self._exec("wtype", text)
            if rc == 0:
                return
        if shutil.which("xdotool"):
            rc, _, _ = await self._exec("xdotool", "type", "--clearmodifiers", "--delay", "8", text)
            if rc == 0:
                return

        def _do_type() -> None:
            from pynput.keyboard import Controller as KeyController

            KeyController().type(text)

        await asyncio.to_thread(_do_type)

    async def check_permissions(self) -> PermissionReport:
        mic_ok, uinput_writable = await asyncio.to_thread(
            lambda: (os.path.exists("/dev/snd"), os.access("/dev/uinput", os.W_OK))
        )
        rc_a11y, _, _ = await self._exec(
            self._sys_python,
            "-c",
            "import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi; Atspi.init()",
        )
        a11y_ok = rc_a11y == 0
        screen_ok = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        x11_avail = bool(os.environ.get("DISPLAY"))
        input_ok = uinput_writable or x11_avail

        return PermissionReport(
            microphone=mic_ok,
            accessibility=a11y_ok,
            screen_recording=screen_ok,
            input_synthesis=input_ok,
            details={
                "session_type": self._session_type,
                "uinput_writable": str(uinput_writable),
                "xwayland_or_x11": str(x11_avail),
            },
        )
