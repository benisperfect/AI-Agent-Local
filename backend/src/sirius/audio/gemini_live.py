"""Google Gemini 3.8 Live (<300ms Full-Duplex Voice & Async Tool-Calling) Session Client."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import numpy as np
import structlog
from google import genai
from google.genai import types
from numpy.typing import NDArray

logger = structlog.get_logger(__name__)

TranscriptCallback = Callable[[Literal["user", "assistant"], str, bool], Awaitable[None]]
ToolCallHandler = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
ToolCancelHandler = Callable[[list[str]], Awaitable[None]]
SimpleAsyncCallback = Callable[[], Awaitable[None]]


class GeminiLiveClient:
    """Bidirectional WebSocket client for `gemini-3.8-live` and `gemini-3.8-live-extended-thinking`."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.8-live",
        voice_name: str = "Aoede",
        system_instruction: str = "",
        tool_declarations: list[types.FunctionDeclaration] | None = None,
        on_audio_pcm24k: Callable[[bytes], None] | None = None,
        on_transcript: TranscriptCallback | None = None,
        on_interrupted: SimpleAsyncCallback | None = None,
        on_turn_complete: SimpleAsyncCallback | None = None,
        on_tool_call: ToolCallHandler | None = None,
        on_tool_cancellation: ToolCancelHandler | None = None,
    ) -> None:
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )
        self._model = model
        self._voice_name = voice_name
        self._system_instruction = system_instruction
        self._tool_declarations = tool_declarations or []

        self._on_audio_pcm24k = on_audio_pcm24k
        self._on_transcript = on_transcript
        self._on_interrupted = on_interrupted
        self._on_turn_complete = on_turn_complete
        self._on_tool_call = on_tool_call
        self._on_tool_cancellation = on_tool_cancellation

        self._session_cm: Any | None = None
        self._session: Any | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._reconnect_tasks: set[asyncio.Task[None]] = set()
        self._active_tool_tasks: dict[str, asyncio.Task[None]] = {}
        self._resumption_handle: str | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._session is not None

    @property
    def uses_extended_thinking(self) -> bool:
        return "extended-thinking" in self._model

    async def connect(self) -> None:
        """Open bidirectional Gemini 3.8 Live WebSocket session with compression & resumption."""
        if self._connected:
            return

        tools_cfg: types.ToolListUnion | None = None
        if self._tool_declarations:
            for decl in self._tool_declarations:
                decl.behavior = types.Behavior.NON_BLOCKING
            tools_cfg = [types.Tool(function_declarations=self._tool_declarations)]

        thinking_cfg = (
            types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
            if self.uses_extended_thinking
            else None
        )

        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice_name)
                )
            ),
            thinking_config=thinking_cfg,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
            session_resumption=types.SessionResumptionConfig(
                handle=self._resumption_handle
            ),
            system_instruction=types.Content(
                parts=[types.Part.from_text(text=self._system_instruction)]
            ),
            tools=tools_cfg,
        )

        self._session_cm = self._client.aio.live.connect(model=self._model, config=config)
        self._session = await self._session_cm.__aenter__()
        self._connected = True
        self._recv_task = asyncio.create_task(self._receive_loop(), name="gemini-live-recv")
        logger.info(
            "gemini_live_connected",
            model=self._model,
            voice=self._voice_name,
            resumed=bool(self._resumption_handle),
        )

    async def disconnect(self) -> None:
        """Close Live session and cancel in-flight async tool tasks."""
        self._connected = False
        for task in list(self._active_tool_tasks.values()):
            task.cancel()
        self._active_tool_tasks.clear()

        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task
            self._recv_task = None

        if self._session_cm is not None:
            with contextlib.suppress(Exception):
                await self._session_cm.__aexit__(None, None, None)
            self._session_cm = None
            self._session = None
        logger.info("gemini_live_disconnected")

    async def send_audio_frame(self, frame_16k_f32: NDArray[np.float32]) -> None:
        """Stream a 16kHz float32 PCM frame to Gemini 3.8 Live."""
        if not self.is_connected or self._session is None:
            return
        pcm16 = (np.clip(frame_16k_f32, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm16, mime_type="audio/pcm;rate=16000")
        )

    async def send_audio_stream_end(self) -> None:
        """Signal Hybrid VAD end-of-speech silence to minimize turn-taking latency."""
        if not self.is_connected or self._session is None:
            return
        await self._session.send_realtime_input(audio_stream_end=True)

    async def send_realtime_text(self, text: str) -> None:
        """Stream user text input over `send_realtime_input` per Gemini 3.8 Live spec."""
        if not self.is_connected or self._session is None:
            return
        await self._session.send_realtime_input(text=text)

    async def send_text_turn(self, text: str, end_of_turn: bool = True) -> None:
        """Inject explicit context turn via `send_client_content` (`turn_complete` interrupts if True)."""
        if not self.is_connected or self._session is None:
            return
        content = types.Content(role="user", parts=[types.Part.from_text(text=text)])
        await self._session.send_client_content(turns=content, turn_complete=end_of_turn)

    async def speak_exact_prompt(self, sentence: str) -> None:
        """Instruct Gemini 3.8 Live to speak a concise status/confirmation line verbatim."""
        if not self.is_connected or self._session is None:
            return
        directive = (
            f"[SYSTEM DIRECTIVE — SPEAK THIS ONE SENTENCE NATURALLY AND CONCISELY TO THE USER]: "
            f"{sentence}"
        )
        await self.send_realtime_text(directive)

    async def _rotate_session_on_go_away(self) -> None:
        await self.disconnect()
        with contextlib.suppress(Exception):
            await self.connect()

    async def _receive_loop(self) -> None:
        try:
            while self._connected and self._session is not None:
                async for server_msg in self._session.receive():
                    await self._handle_server_message(server_msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("gemini_live_receive_error", error=str(exc))
            self._connected = False

    async def _handle_server_message(self, msg: types.LiveServerMessage) -> None:
        # 1. Session resumption handle persistence (2-hour validity window)
        if msg.session_resumption_update:
            upd = msg.session_resumption_update
            if upd.resumable and upd.new_handle:
                self._resumption_handle = str(upd.new_handle)

        # 2. Proactive rotation on server GoAway warning
        if msg.go_away is not None:
            logger.info("gemini_live_go_away_rotating_session", time_left=str(msg.go_away.time_left))
            rot_task = asyncio.create_task(self._rotate_session_on_go_away())
            self._reconnect_tasks.add(rot_task)
            rot_task.add_done_callback(self._reconnect_tasks.discard)
            return

        # 3. Server content (audio chunks, interim/final transcripts, interruption, status)
        sc = msg.server_content
        if sc is not None:
            if sc.interrupted and self._on_interrupted is not None:
                await self._on_interrupted()

            if sc.interim_input_transcription and sc.interim_input_transcription.text:
                if self._on_transcript is not None:
                    await self._on_transcript("user", sc.interim_input_transcription.text, False)

            if sc.input_transcription and sc.input_transcription.text:
                if self._on_transcript is not None:
                    await self._on_transcript("user", sc.input_transcription.text, True)

            if sc.output_transcription and sc.output_transcription.text:
                if self._on_transcript is not None:
                    await self._on_transcript("assistant", sc.output_transcription.text, True)

            if sc.model_turn and sc.model_turn.parts:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        if self._on_audio_pcm24k is not None:
                            self._on_audio_pcm24k(bytes(part.inline_data.data))
                    elif part.text and self._on_transcript is not None:
                        await self._on_transcript("assistant", part.text, False)

            # On Extended Thinking models, wait for interaction_status == IDLE; on 3.8 Live, turn_complete
            interaction_status = str(getattr(sc, "interaction_status", "") or getattr(msg, "interaction_status", ""))
            if self.uses_extended_thinking:
                if "IDLE" in interaction_status and self._on_turn_complete is not None:
                    await self._on_turn_complete()
            elif sc.turn_complete and self._on_turn_complete is not None:
                await self._on_turn_complete()

        # 4. Server-canceled tool calls (when user barges in mid-execution)
        if msg.tool_call_cancellation and msg.tool_call_cancellation.ids:
            cancel_ids = [str(cid) for cid in msg.tool_call_cancellation.ids if cid]
            for cid in cancel_ids:
                t = self._active_tool_tasks.pop(cid, None)
                if t is not None and not t.done():
                    t.cancel()
            if self._on_tool_cancellation is not None and cancel_ids:
                await self._on_tool_cancellation(cancel_ids)

        # 5. Asynchronous NON_BLOCKING tool invocations
        if msg.tool_call and msg.tool_call.function_calls:
            for fc in msg.tool_call.function_calls:
                call_id = fc.id or uuid.uuid4().hex[:10]
                fn_name = fc.name or "unknown_tool"
                fn_args = dict(fc.args) if fc.args else {}
                task = asyncio.create_task(
                    self._dispatch_tool_call(call_id, fn_name, fn_args),
                    name=f"live-tool-{call_id}",
                )
                self._active_tool_tasks[call_id] = task

    async def _dispatch_tool_call(
        self,
        call_id: str,
        fn_name: str,
        fn_args: dict[str, Any],
    ) -> None:
        try:
            if self._on_tool_call is None:
                result: dict[str, Any] = {"error": "No tool handler registered"}
            else:
                result = await self._on_tool_call(call_id, fn_name, fn_args)

            if self.is_connected and self._session is not None:
                await self._session.send_tool_response(
                    function_responses=[
                        types.FunctionResponse(
                            id=call_id,
                            name=fn_name,
                            response=result,
                        )
                    ]
                )
        except asyncio.CancelledError:
            logger.info("live_tool_call_cancelled", call_id=call_id, tool=fn_name)
            raise
        except Exception as exc:
            logger.exception("live_tool_call_failed", call_id=call_id, tool=fn_name, error=str(exc))
            if self.is_connected and self._session is not None:
                with contextlib.suppress(Exception):
                    await self._session.send_tool_response(
                        function_responses=[
                            types.FunctionResponse(
                                id=call_id,
                                name=fn_name,
                                response={"ok": False, "error": str(exc)},
                            )
                        ]
                    )
        finally:
            self._active_tool_tasks.pop(call_id, None)
