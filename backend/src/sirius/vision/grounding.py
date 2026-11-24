"""Tier-3 Vision-Language GUI Agent using Set-of-Marks + Gemini 4 Vision Grounding."""

from __future__ import annotations

import base64
from typing import Any

import structlog
from google import genai
from pydantic import BaseModel, Field

from sirius.os_control.base import OSAdapter
from sirius.vision.screen import MonitorCapture, MultiMonitorCapturer
from sirius.vision.som import AnnotatedScreenshot, SetOfMarksAnnotator

logger = structlog.get_logger(__name__)


class VisionGroundingPrediction(BaseModel):
    found: bool
    mark_id: int | None = None
    box_2d: list[int] = Field(
        default_factory=list,
        description="Normalized [ymin, xmin, ymax, xmax] in 0..1000 scale if mark_id is null or needs refinement.",
    )
    confidence: float = 0.0
    reasoning: str = ""


class VisionGUIAgent:
    """Universal visual fallback grounding arbitrary desktop/canvas apps via Gemini Pro Vision."""

    def __init__(
        self,
        adapter: OSAdapter,
        api_key: str | None = None,
        vision_model: str = "gemini-4-hydrogen",
        capturer: MultiMonitorCapturer | None = None,
        annotator: SetOfMarksAnnotator | None = None,
    ) -> None:
        self._adapter = adapter
        self._api_key = api_key
        self._vision_model = vision_model
        self._capturer = capturer or MultiMonitorCapturer()
        self._annotator = annotator or SetOfMarksAnnotator()

    def set_api_key(self, api_key: str | None) -> None:
        self._api_key = api_key

    async def locate_and_act(
        self,
        target_description: str,
        action: str = "click",
        value: str | None = None,
        monitor_index: int = 1,
    ) -> dict[str, Any]:
        """Capture screen, overlay Set-of-Marks, predict coordinates with Gemini Pro Vision, and act."""
        capture = await self._capturer.capture(monitor_index=monitor_index)
        a11y_roots = await self._adapter.get_accessibility_tree(max_depth=3)
        annotated = self._annotator.annotate(
            screenshot=capture.image,
            monitor_bounds=capture.bounds,
            a11y_roots=a11y_roots,
        )

        prediction = await self.predict_element_location(target_description, annotated)
        if not prediction.found:
            return {
                "ok": False,
                "summary": f"Vision grounding could not find '{target_description}': {prediction.reasoning}",
            }

        coords = self.resolve_desktop_coordinates(prediction, capture, annotated)
        if coords is None:
            return {
                "ok": False,
                "summary": f"Vision model returned invalid coordinates for '{target_description}'.",
            }

        gx, gy = coords
        act = action.lower()
        if act in ("click", "left_click", "press"):
            await self._adapter.click(gx, gy, button="left", count=1)
        elif act == "double_click":
            await self._adapter.click(gx, gy, button="left", count=2)
        elif act == "right_click":
            await self._adapter.click(gx, gy, button="right", count=1)
        elif act == "move":
            await self._adapter.move_mouse(gx, gy, smooth=True)
        else:
            await self._adapter.click(gx, gy, button="left", count=1)

        if value is not None:
            await self._adapter.type_text(value)

        logger.info(
            "tier3_vision_action_executed",
            target=target_description,
            action=act,
            x=gx,
            y=gy,
            mark_id=prediction.mark_id,
        )
        return {
            "ok": True,
            "summary": f"Vision grounded '{target_description}' at ({gx}, {gy}) and performed {act}",
            "x": gx,
            "y": gy,
            "mark_id": prediction.mark_id,
            "confidence": prediction.confidence,
        }

    async def predict_element_location(
        self,
        target_description: str,
        annotated: AnnotatedScreenshot,
    ) -> VisionGroundingPrediction:
        if not self._api_key:
            return VisionGroundingPrediction(
                found=False,
                reasoning="GEMINI_API_KEY is not configured for Vision GUI grounding.",
            )

        client = genai.Client(api_key=self._api_key)
        prompt = (
            "You are a precision GUI visual grounding engine.\n"
            f"Locate the UI element matching: {target_description!r}\n"
            "The screenshot includes numbered Set-of-Marks badges (#1, #2, ...).\n"
            f"Known Marks Legend:\n{annotated.marks_legend()}\n\n"
            "If one of the numbered marks accurately covers the target control, set found=true and mark_id to that integer.\n"
            "Also always provide box_2d as [ymin, xmin, ymax, xmax] in normalized 0..1000 image coordinates."
        )

        png_b64 = base64.b64encode(annotated.to_png_bytes()).decode("utf-8")
        interaction = await client.aio.interactions.create(
            model=self._vision_model,
            input=[
                {"type": "text", "text": prompt},
                {"type": "image", "data": png_b64, "mime_type": "image/png"},
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": VisionGroundingPrediction.model_json_schema(),
            },
        )
        out_txt = getattr(interaction, "output_text", None)
        if not out_txt:
            return VisionGroundingPrediction(found=False, reasoning="Empty vision response")
        return VisionGroundingPrediction.model_validate_json(str(out_txt))

    @staticmethod
    def resolve_desktop_coordinates(
        prediction: VisionGroundingPrediction,
        capture: MonitorCapture,
        annotated: AnnotatedScreenshot,
    ) -> tuple[int, int] | None:
        if prediction.mark_id is not None and prediction.mark_id in annotated.marks:
            return annotated.marks[prediction.mark_id].desktop_bounds.center
        if len(prediction.box_2d) == 4:
            return capture.normalized_box_to_desktop_point(prediction.box_2d)
        return None
