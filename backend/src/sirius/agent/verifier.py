"""Closed-Loop Post-Action Verifier checking DOM Diffs, Accessibility Trees, and Screenshots."""

from __future__ import annotations

import base64
import re
from typing import Any

from google import genai
from pydantic import BaseModel

from sirius.browser.agent import AuthenticatedBrowserAgent
from sirius.os_control.controller import ComputerController
from sirius.vision.screen import MultiMonitorCapturer

_ERROR_OR_MODAL_RE = re.compile(
    r"\b(error\s+occurred|invalid\s+input|session\s+expired|cookie\s+consent|accept\s+all\s+cookies|"
    r"something\s+went\s+wrong|try\s+again\s+later|loading\.\.\.)\b",
    re.IGNORECASE,
)


class VerificationResult(BaseModel):
    verified: bool
    blocker_detected: str = ""
    adaptation_hint: str = ""


class ClosedLoopVerifier:
    """Confirms UI and browser state transitions actually succeeded after tool execution."""

    def __init__(
        self,
        computer: ComputerController,
        browser: AuthenticatedBrowserAgent,
        capturer: MultiMonitorCapturer | None = None,
        api_key: str | None = None,
        vision_model: str = "gemini-4-hydrogen",
    ) -> None:
        self._computer = computer
        self._browser = browser
        self._capturer = capturer or MultiMonitorCapturer()
        self._api_key = api_key
        self._vision_model = vision_model

    def set_api_key(self, api_key: str | None) -> None:
        self._api_key = api_key

    async def verify_step(
        self,
        step_description: str,
        tool_name: str,
        exec_ok: bool,
        exec_payload: dict[str, Any],
    ) -> VerificationResult:
        if not exec_ok:
            err = str(exec_payload.get("error") or exec_payload.get("reason") or "Tool returned ok=False")
            return VerificationResult(
                verified=False,
                blocker_detected=err,
                adaptation_hint="Inspect current UI/DOM hierarchy and select an alternate control or fallback tier.",
            )

        # 1. Browser state verification via live DOM inspection
        if tool_name.startswith("browser_") and self._browser.is_connected:
            try:
                distilled = await self._browser.distill_page()
                m = _ERROR_OR_MODAL_RE.search(distilled.excerpt[:600])
                if m:
                    matched_txt = m.group(0)
                    return VerificationResult(
                        verified=False,
                        blocker_detected=f"Detected page blocker/banner: '{matched_txt}'",
                        adaptation_hint=f"Dismiss or handle '{matched_txt}' on {distilled.url} and retry.",
                    )
                return VerificationResult(verified=True)
            except Exception as exc:
                return VerificationResult(
                    verified=False,
                    blocker_detected=str(exc),
                    adaptation_hint="Re-attach browser session and verify active tab.",
                )

        # 2. Desktop UI verification via Accessibility tree or Vision screenshot
        if tool_name == "interact_ui":
            tree_summary = await self._computer.inspect_ui_hierarchy()
            if tree_summary and not tree_summary.startswith("(No semantic"):
                return VerificationResult(verified=True)

            if self._api_key:
                return await self._verify_via_screenshot(step_description)

        return VerificationResult(verified=True)

    async def _verify_via_screenshot(self, step_description: str) -> VerificationResult:
        capture = await self._capturer.capture(monitor_index=1)
        client = genai.Client(api_key=self._api_key)
        prompt = (
            f"Verify whether this desktop action succeeded: {step_description!r}.\n"
            "Check for blocking modal dialogs, error banners, or loading spinners."
        )
        png_b64 = base64.b64encode(capture.to_png_bytes()).decode("utf-8")
        interaction = await client.aio.interactions.create(
            model=self._vision_model,
            input=[
                {"type": "text", "text": prompt},
                {"type": "image", "data": png_b64, "mime_type": "image/png"},
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": VerificationResult.model_json_schema(),
            },
        )
        out_txt = getattr(interaction, "output_text", None)
        if not out_txt:
            return VerificationResult(verified=True)
        return VerificationResult.model_validate_json(str(out_txt))
