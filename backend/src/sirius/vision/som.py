"""Set-of-Marks (SoM) Visual Grounding Annotator with CV Connected-Box Proposals."""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from sirius.os_control.base import AccessibilityNode, Rect


@dataclass(frozen=True)
class SoMMark:
    mark_id: int
    label_hint: str
    local_bounds: Rect
    desktop_bounds: Rect


@dataclass(frozen=True)
class AnnotatedScreenshot:
    image: Image.Image
    marks: dict[int, SoMMark]
    monitor_bounds: Rect

    def to_png_bytes(self) -> bytes:
        buf = io.BytesIO()
        self.image.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def marks_legend(self) -> str:
        if not self.marks:
            return "(No pre-indexed marks; predict normalized box_2d [ymin, xmin, ymax, xmax] 0..1000 directly.)"
        lines = [
            f"[#{m.mark_id}] {m.label_hint or 'ui-region'} @({m.local_bounds.x},{m.local_bounds.y},{m.local_bounds.width}x{m.local_bounds.height})"
            for m in list(self.marks.values())[:90]
        ]
        return "\n".join(lines)


class SetOfMarksAnnotator:
    """Renders numbered high-contrast Set-of-Marks overlays from A11y + OmniParser-style CV proposals."""

    def annotate(
        self,
        screenshot: Image.Image,
        monitor_bounds: Rect,
        a11y_roots: list[AccessibilityNode] | None = None,
        max_marks: int = 75,
    ) -> AnnotatedScreenshot:
        canvas = screenshot.convert("RGB").copy()
        draw = ImageDraw.Draw(canvas)
        w, h = canvas.size

        candidates: list[tuple[Rect, str]] = []

        # 1. Seed with visible Tier-2 Accessibility bounding boxes if available
        if a11y_roots:
            for root in a11y_roots:
                for node in root.flatten():
                    if node.bounds and node.bounds.is_visible() and (node.actions or node.name):
                        lx = node.bounds.x - monitor_bounds.x
                        ly = node.bounds.y - monitor_bounds.y
                        if 0 <= lx < w and 0 <= ly < h and node.bounds.width < int(w * 0.85):
                            candidates.append(
                                (
                                    Rect(lx, ly, node.bounds.width, node.bounds.height),
                                    f"{node.role}:{node.name[:32]}",
                                )
                            )

        # 2. Supplement with CV gradient connected-component UI control proposals (for canvas/custom UIs)
        if len(candidates) < 12:
            cv_boxes = self._propose_ui_boxes_cv(screenshot, limit=max_marks - len(candidates))
            for r in cv_boxes:
                if not any(self._iou(r, existing[0]) > 0.45 for existing in candidates):
                    candidates.append((r, "visual-control"))

        marks: dict[int, SoMMark] = {}
        palette = [
            (239, 68, 68),
            (16, 185, 129),
            (59, 130, 246),
            (245, 158, 11),
            (168, 85, 247),
            (236, 72, 153),
        ]

        for idx, (lrect, hint) in enumerate(candidates[:max_marks], start=1):
            color = palette[(idx - 1) % len(palette)]
            x1, y1 = lrect.x, lrect.y
            x2, y2 = min(w - 1, lrect.x + lrect.width), min(h - 1, lrect.y + lrect.height)
            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)

            tag_txt = str(idx)
            badge_w = 8 + 7 * len(tag_txt)
            badge_h = 15
            bx = max(0, min(x1, w - badge_w))
            by = max(0, y1 - badge_h if y1 >= badge_h else y1)
            draw.rectangle([bx, by, bx + badge_w, by + badge_h], fill=color)
            draw.text((bx + 4, by + 2), tag_txt, fill=(255, 255, 255))

            drect = Rect(
                x=monitor_bounds.x + lrect.x,
                y=monitor_bounds.y + lrect.y,
                width=lrect.width,
                height=lrect.height,
            )
            marks[idx] = SoMMark(
                mark_id=idx,
                label_hint=hint,
                local_bounds=lrect,
                desktop_bounds=drect,
            )

        return AnnotatedScreenshot(image=canvas, marks=marks, monitor_bounds=monitor_bounds)

    @staticmethod
    def _propose_ui_boxes_cv(image: Image.Image, limit: int = 50) -> list[Rect]:
        """Detect crisp button/input/icon bounding boxes using Sobel edge density & connected components."""
        gray = np.asarray(image.convert("L"), dtype=np.float32)
        h, w = gray.shape
        if h < 32 or w < 32:
            return []

        gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
        gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
        edges = (gx + gy) > 28.0

        # Dilate horizontally & vertically so button text/borders merge into compact regions
        struct = np.ones((5, 9), dtype=bool)
        dilated = ndimage.binary_dilation(edges, structure=struct)
        labeled, num_features = ndimage.label(dilated)
        if num_features == 0:
            return []

        slices = ndimage.find_objects(labeled)
        boxes: list[Rect] = []
        for sl in slices:
            if sl is None:
                continue
            ys, xs = sl
            bw = int(xs.stop - xs.start)
            bh = int(ys.stop - ys.start)
            if 18 <= bw <= int(w * 0.45) and 14 <= bh <= int(h * 0.20):
                boxes.append(Rect(int(xs.start), int(ys.start), bw, bh))
                if len(boxes) >= limit:
                    break
        return boxes

    @staticmethod
    def _iou(a: Rect, b: Rect) -> float:
        ix1 = max(a.x, b.x)
        iy1 = max(a.y, b.y)
        ix2 = min(a.x + a.width, b.x + b.width)
        iy2 = min(a.y + a.height, b.y + b.height)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = float((ix2 - ix1) * (iy2 - iy1))
        union = float(a.width * a.height + b.width * b.height) - inter
        return inter / max(union, 1.0)
