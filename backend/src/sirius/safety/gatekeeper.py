"""Safety Gatekeeper coordinating vocal + HUD confirmations for HIGH_IRREVERSIBLE actions."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from sirius.ipc.protocol import AgentState, ConfirmationRequestMessage, RiskLevel
from sirius.ipc.server import IPCServer
from sirius.logging_config import AuditLogger
from sirius.safety.risk_classifier import RiskAssessment, RiskClassifier

logger = structlog.get_logger(__name__)

SpeakPromptFn = Callable[[str], Awaitable[None]]
SetStateFn = Callable[[AgentState, str], Awaitable[None]]


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    assessment: RiskAssessment
    reason: str


class SafetyGatekeeper:
    """Enforces Sirius's auto-run default with vocal confirmation on irreversible actions."""

    def __init__(
        self,
        ipc: IPCServer,
        audit: AuditLogger,
        classifier: RiskClassifier | None = None,
        speak_fn: SpeakPromptFn | None = None,
        set_state_fn: SetStateFn | None = None,
        timeout_seconds: float = 25.0,
    ) -> None:
        self._ipc = ipc
        self._audit = audit
        self._classifier = classifier or RiskClassifier()
        self._speak_fn = speak_fn
        self._set_state_fn = set_state_fn
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, asyncio.Future[tuple[bool, str]]] = {}

    def attach_voice_hooks(
        self,
        speak_fn: SpeakPromptFn,
        set_state_fn: SetStateFn,
    ) -> None:
        self._speak_fn = speak_fn
        self._set_state_fn = set_state_fn

    @property
    def has_pending_confirmation(self) -> bool:
        return len(self._pending) > 0

    async def evaluate_and_authorize(
        self,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        tainted_by_untrusted_content: bool = False,
    ) -> GateDecision:
        assessment = self._classifier.classify(
            tool_name=tool_name,
            arguments=arguments,
            tainted_by_untrusted_content=tainted_by_untrusted_content,
        )

        # 1. BLOCKED -> Refuse immediately
        if assessment.level == RiskLevel.BLOCKED:
            self._audit.record(
                "tool_blocked",
                {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "risk_level": assessment.level.value,
                    "reason": assessment.reason,
                },
            )
            if self._speak_fn is not None:
                await self._speak_fn(assessment.reason)
            return GateDecision(
                allowed=False,
                assessment=assessment,
                reason=assessment.reason,
            )

        # 2. LOW / MEDIUM -> Auto-run immediately without nagging!
        if assessment.level in (RiskLevel.LOW, RiskLevel.MEDIUM):
            self._audit.record(
                "tool_auto_authorized",
                {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "risk_level": assessment.level.value,
                },
            )
            return GateDecision(
                allowed=True,
                assessment=assessment,
                reason="Auto-authorized per low/medium risk policy.",
            )

        # 3. HIGH_IRREVERSIBLE -> Pause task, speak 1-sentence confirmation, wait for Yes/No
        request_id = uuid.uuid4().hex[:10]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[bool, str]] = loop.create_future()
        self._pending[request_id] = fut

        if self._set_state_fn is not None:
            await self._set_state_fn(
                AgentState.AWAITING_CONFIRMATION,
                f"confirm:{tool_name}",
            )

        await self._ipc.broadcast(
            ConfirmationRequestMessage(
                request_id=request_id,
                call_id=call_id,
                tool_name=tool_name,
                risk_level=assessment.level,
                spoken_prompt=assessment.spoken_confirmation_prompt,
                action_details=arguments,
                expires_in_seconds=self._timeout_seconds,
            ),
            roles={"hud", "cli"},
        )

        if self._speak_fn is not None:
            await self._speak_fn(assessment.spoken_confirmation_prompt)

        try:
            approved, source = await asyncio.wait_for(fut, timeout=self._timeout_seconds)
            decision_reason = (
                f"Approved via {source}" if approved else f"Declined via {source}"
            )
        except TimeoutError:
            approved = False
            decision_reason = "Confirmation timed out; cancelled for safety."
        finally:
            self._pending.pop(request_id, None)
            if self._set_state_fn is not None:
                await self._set_state_fn(AgentState.LISTENING, "confirmation_resolved")

        self._audit.record(
            "tool_confirmation_resolved",
            {
                "request_id": request_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "approved": approved,
                "reason": decision_reason,
            },
        )
        return GateDecision(
            allowed=approved,
            assessment=assessment,
            reason=decision_reason,
        )

    async def resolve_confirmation(
        self,
        approved: bool,
        source: str = "voice",
        request_id: str | None = None,
    ) -> bool:
        """Resolve a pending confirmation by explicit request_id (HUD) or most recent (Voice)."""
        if not self._pending:
            return False
        target_id = request_id if (request_id and request_id in self._pending) else next(reversed(self._pending))
        fut = self._pending.get(target_id)
        if fut is not None and not fut.done():
            fut.set_result((approved, source))
            return True
        return False

    def cancel_all_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result((False, "emergency_stop"))
        self._pending.clear()
