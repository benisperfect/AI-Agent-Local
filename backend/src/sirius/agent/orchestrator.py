"""Master Sirius Autonomous Agent Orchestrator connecting Voice, OS, Browser, Vision, Safety & HUD."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any

import structlog

from sirius.agent.planner import AutonomousPlanner, ExecutionPlanSchema
from sirius.agent.tools import SiriusToolRegistry
from sirius.agent.verifier import ClosedLoopVerifier
from sirius.audio.gemini_live import GeminiLiveClient
from sirius.audio.pipeline import VoicePipeline
from sirius.browser.agent import AuthenticatedBrowserAgent
from sirius.config import SecretStore, SiriusSettings
from sirius.ipc.protocol import (
    AgentState,
    ClientMessage,
    ConfirmationResponseMessage,
    EmergencyStopMessage,
    MacroDeleteMessage,
    MacroUpsertMessage,
    MemoryDeleteMessage,
    MemorySnapshotMessage,
    MemoryUpsertMessage,
    RequestSnapshotMessage,
    SleepClientMessage,
    SystemStatusMessage,
    TextCommandMessage,
    TranscriptMessage,
    UpdateSettingsMessage,
    WakeClientMessage,
)
from sirius.ipc.server import IPCServer
from sirius.logging_config import AuditLogger
from sirius.memory.macros import VoiceMacroRegistry
from sirius.memory.store import EncryptedMemoryStore
from sirius.os_control.base import OSAdapter
from sirius.os_control.controller import ComputerController
from sirius.safety.gatekeeper import SafetyGatekeeper
from sirius.vision.grounding import VisionGUIAgent

logger = structlog.get_logger(__name__)


class SiriusOrchestrator:
    """Coordinates full-duplex voice, multi-step planning, closed-loop verification, and HUD IPC."""

    def __init__(
        self,
        settings: SiriusSettings,
        os_adapter: OSAdapter | None = None,
        virtual_audio: bool = False,
    ) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self.secrets = SecretStore(data_dir=settings.data_dir)
        self.audit = AuditLogger(log_dir=settings.data_dir / "logs")

        ipc_token = self.secrets.get_or_create_ipc_token()
        self.ipc = IPCServer(
            host=settings.ipc_host,
            port=settings.ipc_port,
            auth_token=ipc_token,
            on_message=self._handle_ipc_message,
        )

        api_key = self.secrets.get_gemini_api_key()
        self.computer = ComputerController(adapter=os_adapter)
        self.vision = VisionGUIAgent(
            adapter=self.computer.adapter,
            api_key=api_key,
            vision_model=settings.gemini_vision_model,
        )
        self.computer.attach_vision_fallback(self.vision.locate_and_act)

        self.browser = AuthenticatedBrowserAgent(
            ipc=self.ipc,
            primary_browser=settings.primary_browser,
            cdp_host=settings.cdp_host,
            cdp_port=settings.cdp_port,
            downloads_dir=settings.data_dir / "downloads",
        )

        self.memory = EncryptedMemoryStore(
            db_path=settings.data_dir / "memory.sqlite3",
            fernet_key=self.secrets.get_or_create_memory_key(),
            gemini_api_key=api_key,
            embedding_model=settings.gemini_embedding_model,
        )
        self.macros = VoiceMacroRegistry(self.memory)

        self.gatekeeper = SafetyGatekeeper(
            ipc=self.ipc,
            audit=self.audit,
            timeout_seconds=settings.confirmation_timeout_seconds,
        )
        self.tools = SiriusToolRegistry(
            computer=self.computer,
            browser=self.browser,
            memory=self.memory,
            gatekeeper=self.gatekeeper,
            ipc=self.ipc,
        )
        self.planner = AutonomousPlanner(
            memory=self.memory,
            macros=self.macros,
            api_key=api_key,
            planner_model=settings.gemini_vision_model,
        )
        self.verifier = ClosedLoopVerifier(
            computer=self.computer,
            browser=self.browser,
            api_key=api_key,
            vision_model=settings.gemini_vision_model,
        )

        self.voice = VoicePipeline(
            settings=settings,
            ipc=self.ipc,
            on_emergency_stop=self._on_emergency_stop,
            on_barge_in=self._on_barge_in,
            on_offline_command=self.execute_user_intent,
            on_vocal_confirmation=self._on_vocal_confirmation,
            virtual_audio=virtual_audio,
        )
        self.gatekeeper.attach_voice_hooks(
            speak_fn=self.voice.speak_status,
            set_state_fn=self.voice.set_state,
        )

        self._active_plan_task: asyncio.Task[str] | None = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        if api_key and not settings.offline_mode:
            self._wire_gemini_live(api_key)

    def _wire_gemini_live(self, api_key: str) -> None:
        live = GeminiLiveClient(
            api_key=api_key,
            model=self.settings.gemini_live_model,
            voice_name=self.settings.voice_name,
            system_instruction=self.settings.persona_prompt,
            tool_declarations=self.tools.gemini_declarations(),
            on_audio_pcm24k=self.voice.on_live_audio_chunk,
            on_transcript=self.voice.on_live_transcript,
            on_interrupted=self.voice.on_live_interrupted,
            on_turn_complete=self.voice.on_live_turn_complete,
            on_tool_call=self._handle_live_tool_call,
        )
        self.voice.attach_live_client(live)

    async def start(self, enable_global_hotkey: bool = True) -> None:
        await self.ipc.start()
        await self.voice.start(enable_global_hotkey=enable_global_hotkey)
        await self.broadcast_system_status()
        logger.info(
            "sirius_orchestrator_ready",
            ipc_port=self.ipc.bound_port,
            wake_word=self.settings.wake_word,
            primary_browser=self.settings.primary_browser,
        )

    async def stop(self) -> None:
        await self._cancel_active_plan()
        await self.voice.stop()
        await self.browser.close()
        await self.computer.mcp.close_all()
        await self.ipc.stop()
        logger.info("sirius_orchestrator_stopped")

    async def _handle_live_tool_call(
        self,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        await self.voice.set_state(AgentState.EXECUTING, reason=f"live_tool:{tool_name}")
        rec = await self.tools.execute_tool(call_id, tool_name, arguments)
        ver = await self.verifier.verify_step(
            step_description=f"{tool_name}({arguments})",
            tool_name=tool_name,
            exec_ok=rec.ok,
            exec_payload=rec.payload,
        )
        return {
            **rec.payload,
            "verified": ver.verified,
            "blocker_detected": ver.blocker_detected,
            "adaptation_hint": ver.adaptation_hint,
        }

    async def execute_user_intent(self, intent: str) -> str:
        """Decompose and execute a single or multi-step user command with closed-loop verification."""
        self.tools.reset_taint()
        await self.voice.set_state(AgentState.THINKING, reason="planning_intent")

        tool_names = [d.name or "" for d in self.tools.gemini_declarations() if d.name]
        plan = await self.planner.build_plan(
            user_intent=intent,
            available_tool_names=tool_names,
            offline_mode=self.settings.offline_mode or (self.secrets.get_gemini_api_key() is None),
        )

        if not plan.steps:
            return plan.initial_spoken_ack or "Done."

        return await self.execute_plan(plan)

    async def execute_plan(self, plan: ExecutionPlanSchema) -> str:
        """Run a multi-step plan graph with progress speech, safety gating, verification & retries."""
        plan_id = uuid.uuid4().hex[:10]
        statuses: dict[str, str] = {s.step_id: "pending" for s in plan.steps}
        await self.ipc.broadcast(
            self.planner.to_ipc_plan_message(plan_id, plan, statuses),
            roles={"hud", "cli"},
        )

        if plan.initial_spoken_ack:
            await self.voice.speak_status(plan.initial_spoken_ack)

        await self.voice.set_state(AgentState.EXECUTING, reason=f"plan:{plan_id}")
        completed_summaries: list[str] = []

        for step in plan.steps:
            statuses[step.step_id] = "running"
            await self.ipc.broadcast(
                self.planner.to_ipc_plan_message(plan_id, plan, statuses),
                roles={"hud", "cli"},
            )

            if step.spoken_progress:
                await self.voice.speak_status(step.spoken_progress)

            try:
                args = json.loads(step.arguments_json) if step.arguments_json else {}
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}

            max_attempts = 2 if step.requires_verification else 1
            step_succeeded = False

            for attempt in range(1, max_attempts + 1):
                call_id = f"{plan_id}-{step.step_id}-a{attempt}"
                rec = await self.tools.execute_tool(call_id, step.tool_name, args)

                if not step.requires_verification:
                    step_succeeded = rec.ok
                    if step_succeeded:
                        completed_summaries.append(rec.summary)
                    break

                ver = await self.verifier.verify_step(
                    step_description=step.description,
                    tool_name=step.tool_name,
                    exec_ok=rec.ok,
                    exec_payload=rec.payload,
                )
                if ver.verified:
                    step_succeeded = True
                    completed_summaries.append(rec.summary)
                    break

                logger.warning(
                    "step_verification_failed",
                    step_id=step.step_id,
                    attempt=attempt,
                    blocker=ver.blocker_detected,
                )
                if rec.payload.get("blocked_or_declined"):
                    break

            statuses[step.step_id] = "verified" if step_succeeded else "failed"
            await self.ipc.broadcast(
                self.planner.to_ipc_plan_message(plan_id, plan, statuses),
                roles={"hud", "cli"},
            )

            if not step_succeeded:
                return f"Stopped on step '{step.description}'."

        return f"Completed {len(completed_summaries)} step(s): " + "; ".join(completed_summaries[-2:])

    async def _on_emergency_stop(self, source: str) -> None:
        self.audit.record("emergency_stop", {"source": source})
        self.gatekeeper.cancel_all_pending()
        self.computer.abort_all_immediate()
        await self._cancel_active_plan()

    async def _on_barge_in(self, reason: str) -> None:
        self.audit.record("user_barge_in", {"reason": reason})
        await self._cancel_active_plan()

    async def _on_vocal_confirmation(self, approved: bool) -> None:
        await self.gatekeeper.resolve_confirmation(approved=approved, source="voice")

    async def _cancel_active_plan(self) -> None:
        if self._active_plan_task is not None and not self._active_plan_task.done():
            self._active_plan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._active_plan_task
        self._active_plan_task = None

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ------------------------------------------------------------------
    # HUD & CLI Authenticated WebSocket IPC Command Router
    # ------------------------------------------------------------------

    async def _handle_ipc_message(self, msg: ClientMessage, session_id: str) -> None:
        if isinstance(msg, WakeClientMessage):
            await self.voice.wake(reason="hud_wake")
            return

        if isinstance(msg, SleepClientMessage):
            await self.voice.sleep(reason="hud_sleep")
            return

        if isinstance(msg, EmergencyStopMessage):
            await self.voice.trigger_emergency_stop(source=msg.source)
            return

        if isinstance(msg, ConfirmationResponseMessage):
            await self.gatekeeper.resolve_confirmation(
                approved=msg.approved,
                source=msg.source,
                request_id=msg.request_id,
            )
            return

        if isinstance(msg, TextCommandMessage):
            await self.ipc.broadcast(TranscriptMessage(role="user", text=msg.text, final=True))
            spot = await self.voice.handle_spoken_or_typed_phrase(msg.text)
            if spot is not None and spot.kind in ("kill_switch", "sleep_phrase"):
                return
            await self._cancel_active_plan()
            self._active_plan_task = asyncio.create_task(self._run_text_command(msg.text))
            return

        if isinstance(msg, MemoryUpsertMessage):
            await self.memory.upsert_memory(msg.category, msg.key, msg.value)
            await self.broadcast_memory_snapshot()
            await self.broadcast_system_status()
            return

        if isinstance(msg, MemoryDeleteMessage):
            await self.memory.delete_memory(msg.memory_id)
            await self.broadcast_memory_snapshot()
            await self.broadcast_system_status()
            return

        if isinstance(msg, MacroUpsertMessage):
            await self.memory.upsert_macro(msg.trigger_phrase, msg.description, msg.steps)
            await self.broadcast_memory_snapshot()
            await self.broadcast_system_status()
            return

        if isinstance(msg, MacroDeleteMessage):
            await self.memory.delete_macro(msg.macro_id)
            await self.broadcast_memory_snapshot()
            await self.broadcast_system_status()
            return

        if isinstance(msg, UpdateSettingsMessage):
            await self._apply_settings_update(msg)
            return

        if isinstance(msg, RequestSnapshotMessage):
            await self.broadcast_system_status(target_session=session_id)
            await self.broadcast_memory_snapshot(target_session=session_id)

    async def _run_text_command(self, text: str) -> str:
        try:
            reply = await self.execute_user_intent(text)
            if reply:
                await self.voice.speak_status(reply)
            if self.voice.state not in (AgentState.SLEEPING, AgentState.AWAITING_CONFIRMATION):
                await self.voice.set_state(AgentState.LISTENING, reason="command_finished")
            return reply
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("text_command_failed", error=str(exc))
            return str(exc)

    async def _apply_settings_update(self, msg: UpdateSettingsMessage) -> None:
        if msg.wake_word:
            self.settings.wake_word = msg.wake_word.strip().lower()
            self.voice.spotter.update_wake_word(self.settings.wake_word)
        if msg.voice_name:
            self.settings.voice_name = msg.voice_name
        if msg.primary_browser:
            self.settings.primary_browser = msg.primary_browser
            self.browser.set_primary_browser(msg.primary_browser)
        if msg.offline_mode is not None:
            self.settings.offline_mode = msg.offline_mode
        if msg.api_key:
            key = msg.api_key.strip()
            self.secrets.set_secret("GEMINI_API_KEY", key)
            self.vision.set_api_key(key)
            self.planner.set_api_key(key)
            self.verifier.set_api_key(key)
            self.memory.set_api_key(key)
            if msg.offline_mode is None:
                self.settings.offline_mode = False
            self._wire_gemini_live(key)
            if self.voice.state != AgentState.SLEEPING and self.voice.live_client is not None:
                with contextlib.suppress(Exception):
                    await self.voice.live_client.connect()
        await self.ipc.broadcast(
            TranscriptMessage(
                role="system",
                text=(
                    f"Config applied: wake='{self.settings.wake_word}', "
                    f"browser={self.settings.primary_browser}, "
                    f"mode={'offline' if self.settings.offline_mode else 'gemini-live'}"
                ),
                final=True,
            ),
            roles={"hud", "cli"},
        )
        await self.broadcast_system_status()

    async def broadcast_system_status(self, target_session: str | None = None) -> None:
        perms = await self.computer.adapter.check_permissions()
        await self.browser.attach_if_available()
        mems = await self.memory.list_memories()
        macros = await self.memory.list_macros()

        status_msg = SystemStatusMessage(
            os_name=self.computer.adapter.os_name,
            wake_word=self.settings.wake_word,
            cloud_provider=self.settings.cloud_provider,
            offline_mode=self.settings.offline_mode,
            browser_connected=self.browser.is_connected,
            browser_mode=self.browser.mode,
            active_browser=self.settings.primary_browser,
            permissions=perms.to_bool_map(),
            memory_count=len(mems),
            macro_count=len(macros),
        )
        if target_session:
            await self.ipc.send_to_session(target_session, status_msg)
        else:
            await self.ipc.broadcast(status_msg, roles={"hud", "cli"})

    async def broadcast_memory_snapshot(self, target_session: str | None = None) -> None:
        snap = MemorySnapshotMessage(
            memories=await self.memory.list_memories(),
            macros=await self.memory.list_macros(),
        )
        if target_session:
            await self.ipc.send_to_session(target_session, snap)
        else:
            await self.ipc.broadcast(snap, roles={"hud", "cli"})
