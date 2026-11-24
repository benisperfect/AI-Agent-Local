"""Fast Multi-Monitor DPI-Aware Screen Capture & Coordinate Normalization (`mss`)."""

from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
from dataclasses import dataclass

import mss
from PIL import Image

from sirius.os_control.base import Rect


@dataclass(frozen=True)
class MonitorCapture:
    monitor_index: int
    bounds: Rect
    image: Image.Image

    def to_png_bytes(self) -> bytes:
        buf = io.BytesIO()
        self.image.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def normalized_box_to_desktop_point(
        self,
        box_2d: tuple[int, int, int, int] | list[int],
    ) -> tuple[int, int]:
        """Convert Gemini 0..1000 [ymin, xmin, ymax, xmax] normalized box to global desktop (x, y)."""
        ymin, xmin, ymax, xmax = (
            max(0, min(1000, int(box_2d[0]))),
            max(0, min(1000, int(box_2d[1]))),
            max(0, min(1000, int(box_2d[2]))),
            max(0, min(1000, int(box_2d[3]))),
        )
        cy_norm = (ymin + ymax) / 2000.0
        cx_norm = (xmin + xmax) / 2000.0
        gx = self.bounds.x + round(cx_norm * self.bounds.width)
        gy = self.bounds.y + round(cy_norm * self.bounds.height)
        return (gx, gy)


class MultiMonitorCapturer:
    """Captures physical monitor viewports across Windows, macOS, and Linux (X11/Wayland)."""

    async def list_monitors(self) -> list[Rect]:
        return await asyncio.to_thread(self._list_monitors_sync)

    @staticmethod
    def _list_monitors_sync() -> list[Rect]:
        mss_cls = getattr(mss, "MSS", mss.mss)
        try:
            with mss_cls() as sct:
                # sct.monitors[0] is combined virtual desktop; 1..N are physical monitors
                physical = sct.monitors[1:] or sct.monitors
                return [
                    Rect(
                        x=int(m["left"]),
                        y=int(m["top"]),
                        width=int(m["width"]),
                        height=int(m["height"]),
                    )
                    for m in physical
                ]
        except Exception:
            return [Rect(0, 0, 1920, 1080)]

    async def capture(self, monitor_index: int = 1) -> MonitorCapture:
        try:
            return await asyncio.to_thread(self._capture_mss_sync, monitor_index)
        except Exception:
            return await self._capture_wayland_fallback()

    @staticmethod
    def _capture_mss_sync(monitor_index: int) -> MonitorCapture:
        mss_cls = getattr(mss, "MSS", mss.mss)
        with mss_cls() as sct:
            idx = max(1, min(monitor_index, len(sct.monitors) - 1)) if len(sct.monitors) > 1 else 0
            mon = sct.monitors[idx]
            raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            bounds = Rect(
                x=int(mon["left"]),
                y=int(mon["top"]),
                width=int(mon["width"]),
                height=int(mon["height"]),
            )
            return MonitorCapture(monitor_index=idx, bounds=bounds, image=img)

    async def _capture_wayland_fallback(self) -> MonitorCapture:
        """Fallback for strict Wayland compositors (`grim` or `gnome-screenshot`)."""
        tmp_png = await asyncio.to_thread(
            lambda: tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
        )
        for tool, args in (
            ("grim", [tmp_png]),
            ("gnome-screenshot", ["-f", tmp_png]),
        ):
            resolved = shutil.which(tool)
            if not resolved:
                continue
            proc = await asyncio.create_subprocess_exec(
                resolved,
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                img = await asyncio.to_thread(self._load_and_unlink_sync, tmp_png)
                if img is not None:
                    return MonitorCapture(
                        monitor_index=1,
                        bounds=Rect(0, 0, img.width, img.height),
                        image=img,
                    )

        # Headless synthetic fallback canvas
        blank = Image.new("RGB", (1280, 720), color=(20, 24, 34))
        return MonitorCapture(monitor_index=1, bounds=Rect(0, 0, 1280, 720), image=blank)

    @staticmethod
    def _load_and_unlink_sync(path_str: str) -> Image.Image | None:
        import os

        try:
            with Image.open(path_str) as im:
                loaded = im.convert("RGB")
            os.unlink(path_str)
            return loaded
        except Exception:
            return None
