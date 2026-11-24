"""Autonomous Multi-Step Planner with Gemini 2.5 Pro + Offline Deterministic OS Fallback."""

from __future__ import annotations

import re
import uuid
from typing import Any

from google import genai
from pydantic import BaseModel, Field

from sirius.ipc.protocol import ControlTier, PlanStepModel, PlanUpdateMessage
from sirius.memory.macros import VoiceMacroRegistry
from sirius.memory.store import EncryptedMemoryStore


class PlannedStepSchema(BaseModel):
    step_id: str
    description: str
    tool_name: str
    arguments_json: str = Field(
        default="{}",
        description="JSON-encoded dictionary of arguments for tool_name.",
    )
    tier: ControlTier = ControlTier.TIER1_NATIVE
    requires_verification: bool = False
    spoken_progress: str = ""


class ExecutionPlanSchema(BaseModel):
    goal: str
    initial_spoken_ack: str = ""
    steps: list[PlannedStepSchema]


class AutonomousPlanner:
    """Decomposes compound spoken commands into executable, verifiable tool step graphs."""

    def __init__(
        self,
        memory: EncryptedMemoryStore,
        macros: VoiceMacroRegistry,
        api_key: str | None = None,
        planner_model: str = "gemini-4-hydrogen",
    ) -> None:
        self._memory = memory
        self._macros = macros
        self._api_key = api_key
        self._planner_model = planner_model

    def set_api_key(self, api_key: str | None) -> None:
        self._api_key = api_key

    async def build_plan(
        self,
        user_intent: str,
        available_tool_names: list[str],
        offline_mode: bool = False,
    ) -> ExecutionPlanSchema:
        # 1. Check if the user is teaching Sirius a new custom voice macro
        learned = await self._macros.try_learn_from_utterance(user_intent)
        if learned is not None:
            return ExecutionPlanSchema(
                goal=f"Learn voice macro '{learned.trigger_phrase}'",
                initial_spoken_ack=(
                    f"Got it—whenever you say '{learned.trigger_phrase}', I'll run those {len(learned.steps)} steps."
                ),
                steps=[],
            )

        # 2. Expand stored voice macro if matched
        matched_macro = await self._macros.match_utterance(user_intent)
        effective_intent = user_intent
        if matched_macro is not None:
            joined = "; then ".join(matched_macro.steps)
            effective_intent = f"Execute macro '{matched_macro.trigger_phrase}': {joined}"

        # 3. Retrieve relevant personal memories (contact aliases, favorite folders, preferences)
        memories = await self._memory.search_semantic(effective_intent, top_k=6)
        mem_context = "\n".join(
            f"- [{m.category}] {m.key}: {m.value}" for m in memories
        ) or "(none)"

        # 4. Cloud Gemini 2.5 Pro structured step-graph planning
        if not offline_mode and self._api_key:
            return await self._plan_with_gemini(
                effective_intent,
                mem_context,
                available_tool_names,
            )

        # 5. Offline deterministic OS & macro fallback planner
        if matched_macro is not None:
            steps: list[PlannedStepSchema] = []
            for idx, sub_cmd in enumerate(matched_macro.steps, start=1):
                sub_plan = self._plan_offline_single(sub_cmd, step_prefix=f"s{idx}")
                steps.extend(sub_plan)
            return ExecutionPlanSchema(
                goal=matched_macro.description,
                initial_spoken_ack=f"Starting {matched_macro.trigger_phrase}.",
                steps=steps,
            )

        offline_steps = self._plan_offline_single(effective_intent, step_prefix="s1")
        if offline_steps:
            return ExecutionPlanSchema(
                goal=effective_intent,
                initial_spoken_ack="Right away.",
                steps=offline_steps,
            )

        local_reply = self._offline_conversational_reply(effective_intent)
        return ExecutionPlanSchema(
            goal=effective_intent,
            initial_spoken_ack=local_reply,
            steps=[],
        )

    async def _plan_with_gemini(
        self,
        intent: str,
        memory_context: str,
        available_tools: list[str],
    ) -> ExecutionPlanSchema:
        client = genai.Client(api_key=self._api_key)
        sys_prompt = (
            "You are Sirius's Principal Autonomous Task Planner.\n"
            "Decompose the user's spoken request into a minimal, reliable sequence of tool steps.\n"
            "Always prefer Tier-1 native OS/shell/file tools first, Tier-2 Accessibility (`interact_ui`) second, "
            "and Authenticated Browser tools (`browser_open_url`, `browser_distill_page`, `browser_click`, `browser_fill`) for websites.\n"
            "Set requires_verification=true on UI clicks, form submissions, and window/browser state mutations.\n"
            "Keep initial_spoken_ack and spoken_progress calm, warm, and concise (at most 1 short sentence).\n"
            f"Available tools: {', '.join(available_tools)}\n"
            f"User Personal Memory & Aliases:\n{memory_context}"
        )
        interaction = await client.aio.interactions.create(
            model=self._planner_model,
            system_instruction=sys_prompt,
            input=intent,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": ExecutionPlanSchema.model_json_schema(),
            },
        )
        out_txt = getattr(interaction, "output_text", None)
        if not out_txt:
            return ExecutionPlanSchema(goal=intent, steps=[])
        return ExecutionPlanSchema.model_validate_json(str(out_txt))

    @staticmethod
    def _normalize_offline_utterance(utterance: str) -> str:
        low = utterance.strip().lower()
        low = re.sub(r"[?.!,]+$", "", low).strip()
        low = re.sub(
            r"^(?:(?:hey\s+)?sirius[\s,]+)?(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)*",
            "",
            low,
        ).strip()
        return low

    @classmethod
    def _offline_conversational_reply(cls, utterance: str) -> str:
        from datetime import datetime

        low = cls._normalize_offline_utterance(utterance)
        if not low or len(low) < 2:
            return ""
        if any(g in low for g in ("hello", "hi ", "hey", "are you there", "good morning", "good afternoon", "good evening")) or low == "hi":
            return "Hello, I'm listening and ready."
        if "what time" in low or "current time" in low:
            return f"It is currently {datetime.now().strftime('%I:%M %p').lstrip('0')}."
        if "what date" in low or "today's date" in low or "what day" in low:
            return f"Today is {datetime.now().strftime('%A, %B %d, %Y')}."
        return f"I heard '{utterance.strip()}'. In offline mode I can open or close apps, adjust volume or brightness, control media, or run saved voice macros. Add a Gemini API key in Settings for full open-ended conversations."

    @classmethod
    def _plan_offline_single(cls, utterance: str, step_prefix: str = "s1") -> list[PlannedStepSchema]:
        """Parse natural offline spoken OS commands into deterministic Tier-1/Browser tool steps."""
        import json
        from urllib.parse import quote_plus

        low = cls._normalize_offline_utterance(utterance)
        if not low:
            return []

        m_vol = re.search(r"volume\s+(?:to\s+)?(\d{1,3})", low)
        if m_vol:
            pct = max(0, min(100, int(m_vol.group(1))))
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description=f"Set system volume to {pct}%",
                    tool_name="set_volume",
                    arguments_json=json.dumps({"percent": pct}),
                    tier=ControlTier.TIER1_NATIVE,
                )
            ]

        if any(p in low for p in ("volume up", "turn up the volume", "turn up volume", "louder")):
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description="Raise system volume to 75%",
                    tool_name="set_volume",
                    arguments_json=json.dumps({"percent": 75}),
                    tier=ControlTier.TIER1_NATIVE,
                )
            ]

        if any(p in low for p in ("volume down", "turn down the volume", "turn down volume", "quieter")):
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description="Lower system volume to 30%",
                    tool_name="set_volume",
                    arguments_json=json.dumps({"percent": 30}),
                    tier=ControlTier.TIER1_NATIVE,
                )
            ]

        if "unmute" in low:
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description="Unmute audio",
                    tool_name="set_mute",
                    arguments_json=json.dumps({"muted": False}),
                    tier=ControlTier.TIER1_NATIVE,
                )
            ]

        if "mute" in low:
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description="Mute audio",
                    tool_name="set_mute",
                    arguments_json=json.dumps({"muted": True}),
                    tier=ControlTier.TIER1_NATIVE,
                )
            ]

        m_bright = re.search(r"brightness\s+(?:to\s+)?(\d{1,3})", low)
        if m_bright:
            pct = max(5, min(100, int(m_bright.group(1))))
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description=f"Set brightness to {pct}%",
                    tool_name="set_brightness",
                    arguments_json=json.dumps({"percent": pct}),
                    tier=ControlTier.TIER1_NATIVE,
                )
            ]

        for phrase, media_act in (
            ("pause", "play_pause"),
            ("resume", "play_pause"),
            ("play music", "play_pause"),
            ("next track", "next"),
            ("skip track", "next"),
            ("previous track", "previous"),
        ):
            if phrase in low:
                return [
                    PlannedStepSchema(
                        step_id=step_prefix,
                        description=f"Media {media_act}",
                        tool_name="media_control",
                        arguments_json=json.dumps({"action": media_act}),
                        tier=ControlTier.TIER1_NATIVE,
                    )
                ]

        m_search = re.match(r"^(?:search(?:\s+for)?|google)\s+(.+)$", low)
        if m_search:
            q = m_search.group(1).strip()
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description=f"Search web for '{q}'",
                    tool_name="browser_open_url",
                    arguments_json=json.dumps(
                        {"url": f"https://www.google.com/search?q={quote_plus(q)}", "foreground": True}
                    ),
                    tier=ControlTier.BROWSER_CDP,
                )
            ]

        m_focus = re.match(r"^(?:focus|switch\s+to)\s+(.+)$", low)
        if m_focus:
            target = m_focus.group(1).strip()
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description=f"Focus {target}",
                    tool_name="focus_window",
                    arguments_json=json.dumps({"title_or_app": target}),
                    tier=ControlTier.TIER1_NATIVE,
                )
            ]

        m_close = re.match(r"^(?:close|quit)\s+(.+)$", low)
        if m_close:
            target = m_close.group(1).strip()
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description=f"Close {target}",
                    tool_name="close_window",
                    arguments_json=json.dumps({"title_or_app": target}),
                    tier=ControlTier.TIER1_NATIVE,
                )
            ]

        m_open = re.match(r"^(?:open|launch|start|go\s+to)\s+(.+)$", low)
        if m_open:
            target = m_open.group(1).strip()
            if "." in target and " " not in target:
                return [
                    PlannedStepSchema(
                        step_id=step_prefix,
                        description=f"Open website {target}",
                        tool_name="browser_open_url",
                        arguments_json=json.dumps({"url": target, "foreground": True}),
                        tier=ControlTier.BROWSER_CDP,
                        requires_verification=True,
                    )
                ]
            return [
                PlannedStepSchema(
                    step_id=step_prefix,
                    description=f"Launch application {target}",
                    tool_name="launch_app",
                    arguments_json=json.dumps({"app_name": target}),
                    tier=ControlTier.TIER1_NATIVE,
                )
            ]

        return []

    @staticmethod
    def to_ipc_plan_message(
        plan_id: str | None,
        plan: ExecutionPlanSchema,
        statuses: dict[str, Any] | None = None,
    ) -> PlanUpdateMessage:
        pid = plan_id or uuid.uuid4().hex[:10]
        st_map = statuses or {}
        return PlanUpdateMessage(
            plan_id=pid,
            goal=plan.goal,
            steps=[
                PlanStepModel(
                    step_id=s.step_id,
                    description=s.description,
                    tier=s.tier,
                    status=st_map.get(s.step_id, "pending"),
                )
                for s in plan.steps
            ],
        )
