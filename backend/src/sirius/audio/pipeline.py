"""Full-Duplex Hybrid Voice State Machine with Instant Barge-In and <100ms Kill-Switch."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import numpy as np
import structlog
from numpy.typing import NDArray

from sirius.audio.aec import AcousticEchoCanceller
from sirius.audio.gemini_live import GeminiLiveClient
from sirius.audio.io_stream import AudioIOStream
from sirius.audio.local_voice import LocalVoiceEngine
from sirius.audio.vad import SileroVAD
from sirius.audio.wake_kill import GlobalHotkeyKillSwitch, LocalWakeAndKillDetector, SpotEvent
from sirius.config import SiriusSettings
from sirius.ipc.protocol import (
    AgentState,
    AudioLevelMessage,
    StateChangedMessage,
    TranscriptMessage,
)
from sirius.ipc.server import IPCServer

logger = structlog.get_logger(__name__)

EmergencyStopCallback = Callable[[str], Awaitable[None]]
OfflineCommandCallback = Callable[[str], Awaitable[str]]
VocalConfirmCallback = Callable[[bool], Awaitable[None]]


class VoicePipeline:
    """Orchestrates SLEEPING -> LISTENING <-> THINKING/EXECUTING <-> SPEAKING with barge-in."""

    def __init__(
        self,
        settings: SiriusSettings,
        ipc: IPCServer,
        aec: AcousticEchoCanceller | None = None,
        vad: SileroVAD | None = None,
        spotter: LocalWakeAndKillDetector | None = None,
        audio_io: AudioIOStream | None = None,
        local_voice: LocalVoiceEngine | None = None,
        live_client: GeminiLiveClient | None = None,
        on_emergency_stop: EmergencyStopCallback | None = None,
        on_barge_in: EmergencyStopCallback | None = None,
        on_offline_command: OfflineCommandCallback | None = None,
        on_vocal_confirmation: VocalConfirmCallback | None = None,
        virtual_audio: bool = False,
    ) -> None:
        self._settings = settings
        self._ipc = ipc
        models_dir = settings.data_dir / "models"

        self.aec = aec or AcousticEchoCanceller(
            frame_size=512,
            mic_rate=settings.mic_sample_rate,
            speaker_rate=settings.speaker_sample_rate,
        )
        self.vad = vad or SileroVAD(
            models_dir=models_dir,
            sample_rate=settings.mic_sample_rate,
            threshold=settings.vad_threshold,
            auto_download=False,
        )
        self.spotter = spotter or LocalWakeAndKillDetector(
            models_dir=models_dir,
            wake_phrases=list(settings.wake_aliases),
            kill_words=list(settings.kill_words),
            sleep_phrases=list(settings.sleep_phrases),
            sample_rate=settings.mic_sample_rate,
            auto_download=False,
        )
        self.audio_io = audio_io or AudioIOStream(
            aec=self.aec,
            mic_rate=settings.mic_sample_rate,
            speaker_rate=settings.speaker_sample_rate,
            virtual_mode=virtual_audio,
        )
        self.local_voice = local_voice or LocalVoiceEngine(
            models_dir=models_dir,
            whisper_model_size=settings.whisper_model_size,
            audio_io=self.audio_io,
        )
        self.live_client = live_client

        self._on_emergency_stop = on_emergency_stop
        self._on_barge_in = on_barge_in
        self._on_offline_command = on_offline_command
        self._on_vocal_confirmation = on_vocal_confirmation

        self._state: AgentState = AgentState.SLEEPING
        self._last_activity_ts: float = time.monotonic()
        self._wake_grace_until: float = 0.0
        self._consecutive_speech_frames: int = 0
        self._consecutive_silence_frames: int = 0
        self._offline_speech_buffer: list[NDArray[np.float32]] = []
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._frame_counter: int = 0

        self._hotkey = GlobalHotkeyKillSwitch(
            hotkey=settings.emergency_hotkey,
            on_trigger=self._schedule_emergency_stop,
        )

    @property
    def state(self) -> AgentState:
        return self._state

    def attach_live_client(self, client: GeminiLiveClient) -> None:
        self.live_client = client

    async def start(self, enable_global_hotkey: bool = True) -> None:
        if self._running:
            return
        self._running = True
        await self.audio_io.start()
        if enable_global_hotkey:
            self._hotkey.start(asyncio.get_running_loop())
        self._loop_task = asyncio.create_task(self._audio_frame_loop(), name="sirius-voice-loop")
        logger.info("voice_pipeline_started", wake_word=self._settings.wake_word)

    async def stop(self) -> None:
        self._running = False
        self._hotkey.stop()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        if self.live_client is not None and self.live_client.is_connected:
            await self.live_client.disconnect()
        await self.audio_io.stop()
        logger.info("voice_pipeline_stopped")

    async def set_state(self, new_state: AgentState, reason: str) -> None:
        if new_state == self._state:
            return
        prev = self._state
        self._state = new_state
        self._last_activity_ts = time.monotonic()
        logger.info("agent_state_transition", previous=prev.value, current=new_state.value, reason=reason)
        await self._ipc.broadcast(
            StateChangedMessage(
                state=new_state,
                previous_state=prev,
                reason=reason,
            )
        )

    async def wake(self, reason: str = "wake_word") -> None:
        """Transition from SLEEPING into continuous hands-free LISTENING mode."""
        self.vad.reset_states()
        self._consecutive_speech_frames = 0
        self._consecutive_silence_frames = 0
        self._offline_speech_buffer.clear()
        self._wake_grace_until = (
            time.monotonic() + 0.35 if reason.startswith("wake_word") else 0.0
        )
        await self.set_state(AgentState.LISTENING, reason=reason)

        if (
            not self._settings.offline_mode
            and self.live_client is not None
            and not self.live_client.is_connected
        ):
            try:
                await self.live_client.connect()
            except Exception as exc:
                logger.warning("gemini_live_connect_failed_using_offline_fallback", error=str(exc))
                await self._ipc.broadcast(
                    TranscriptMessage(
                        role="system",
                        text=f"Gemini Live unavailable ({exc}) — listening in local offline mode.",
                        final=True,
                    ),
                    roles={"hud", "cli"},
                )

    async def sleep(self, reason: str = "sleep_command") -> None:
        """Return Sirius to local wake-word SLEEPING mode and close cloud stream."""
        self.audio_io.flush_playback()
        self.local_voice.stop_speaking()
        self._offline_speech_buffer.clear()
        if self.live_client is not None and self.live_client.is_connected:
            await self.live_client.disconnect()
        await self.set_state(AgentState.SLEEPING, reason=reason)

    async def trigger_emergency_stop(self, source: str = "local_kill_switch") -> None:
        """Abort all audio playback and active desktop/browser tasks in <100ms."""
        self.audio_io.flush_playback()
        self.local_voice.stop_speaking()
        self._offline_speech_buffer.clear()
        self._consecutive_speech_frames = 0

        if self._state == AgentState.AWAITING_CONFIRMATION and self._on_vocal_confirmation:
            await self._on_vocal_confirmation(False)

        if self._on_emergency_stop is not None:
            await self._on_emergency_stop(source)

        target_state = (
            AgentState.SLEEPING if self._state == AgentState.SLEEPING else AgentState.LISTENING
        )
        await self.set_state(target_state, reason=f"emergency_stop:{source}")
        await self._ipc.broadcast(
            TranscriptMessage(
                role="system",
                text=f"Stopped immediately ({source}).",
                final=True,
            )
        )

    async def handle_spoken_or_typed_phrase(self, text: str) -> SpotEvent | None:
        """Evaluate local phrase spotter rules on recognized transcript or synthetic input."""
        event = self.spotter.classify_phrase(text)
        if event is not None:
            await self._handle_spot_event(event)
        return event

    async def speak_status(self, sentence: str) -> None:
        """Speak a concise status or safety prompt via Gemini Live or offline TTS fallback."""
        clean = sentence.strip()
        if not clean:
            return
        await self._ipc.broadcast(TranscriptMessage(role="assistant", text=clean, final=True))
        if (
            not self._settings.offline_mode
            and self.live_client is not None
            and self.live_client.is_connected
        ):
            await self.live_client.speak_exact_prompt(clean)
        else:
            prev = self._state
            if prev != AgentState.AWAITING_CONFIRMATION:
                await self.set_state(AgentState.SPEAKING, reason="local_tts")
            try:
                await self.local_voice.speak(clean)
            finally:
                if self._state == AgentState.SPEAKING:
                    await self.set_state(AgentState.LISTENING, reason="local_tts_complete")

    def _spawn_bg(self, coro: Awaitable[None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def on_live_audio_chunk(self, pcm24k_bytes: bytes) -> None:
        """Receive 24kHz PCM chunk from Gemini Live and enqueue for speaker output."""
        if self._state not in (AgentState.SPEAKING, AgentState.AWAITING_CONFIRMATION):
            self._spawn_bg(self.set_state(AgentState.SPEAKING, reason="live_audio_output"))
        self.audio_io.enqueue_pcm24k_bytes(pcm24k_bytes, sample_rate=24000)

    async def on_live_interrupted(self) -> None:
        """Handle server-detected barge-in from Gemini Live."""
        self.audio_io.flush_playback()
        if self._on_barge_in is not None:
            await self._on_barge_in("gemini_live_interrupted")
        await self.set_state(AgentState.LISTENING, reason="gemini_live_barge_in")

    async def on_live_turn_complete(self) -> None:
        if self._state == AgentState.SPEAKING:
            await self.set_state(AgentState.LISTENING, reason="turn_complete")

    async def on_live_transcript(
        self,
        role: Literal["user", "assistant"],
        text: str,
        is_final: bool,
    ) -> None:
        self._last_activity_ts = time.monotonic()
        await self._ipc.broadcast(TranscriptMessage(role=role, text=text, final=is_final))
        if role == "user":
            event = self.spotter.classify_phrase(text)
            if event is not None and event.kind in (
                "kill_switch",
                "sleep_phrase",
                "confirm_yes",
                "confirm_no",
            ):
                await self._handle_spot_event(event)

    def _schedule_emergency_stop(self, source: str) -> None:
        if self._running:
            self._spawn_bg(self.trigger_emergency_stop(source=source))

    async def _handle_spot_event(self, event: SpotEvent) -> None:
        if event.kind == "kill_switch":
            await self.trigger_emergency_stop(source=f"voice:{event.phrase}")
            return

        if event.kind == "wake_word" and self._state == AgentState.SLEEPING:
            await self.wake(reason=f"wake_word:{event.phrase}")
            return

        if event.kind == "sleep_phrase" and self._state != AgentState.SLEEPING:
            await self.sleep(reason=f"sleep_phrase:{event.phrase}")
            return

        if self._state == AgentState.AWAITING_CONFIRMATION and self._on_vocal_confirmation:
            if event.kind == "confirm_yes":
                await self._on_vocal_confirmation(True)
            elif event.kind == "confirm_no":
                await self._on_vocal_confirmation(False)

    async def _audio_frame_loop(self) -> None:
        """Continuous 32ms frame processor running AEC -> Spotter -> Silero VAD -> Cloud/Offline."""
        while self._running:
            try:
                frame = await asyncio.wait_for(self.audio_io.read_clean_mic_frame(), timeout=0.5)
            except TimeoutError:
                await self._check_conversation_timeout()
                continue

            self._frame_counter += 1

            # 1. Instant local keyword spotter (Wake-word in SLEEPING, Kill-switch in active states)
            spot_event = self.spotter.process_pcm_frame(frame)
            if spot_event is not None:
                await self._handle_spot_event(spot_event)
                if spot_event.kind == "kill_switch":
                    continue

            # 2. Silero VAD speech probability
            is_speech, vad_prob = self.vad.is_speech(frame)

            # Telemetry to HUD ~10Hz (every 3rd 32ms frame)
            if self._frame_counter % 3 == 0:
                await self._ipc.broadcast(
                    AudioLevelMessage(
                        mic_rms=round(self.audio_io.last_mic_rms, 5),
                        speaker_rms=round(self.audio_io.last_speaker_rms, 5),
                        vad_probability=round(vad_prob, 3),
                        aec_attenuation_db=self.aec.last_attenuation_db,
                    ),
                    roles={"hud"},
                )

            if self._state == AgentState.SLEEPING:
                continue

            if is_speech:
                self._consecutive_speech_frames += 1
                self._consecutive_silence_frames = 0
                self._last_activity_ts = time.monotonic()
            else:
                self._consecutive_silence_frames += 1
                self._consecutive_speech_frames = 0

            # 3. Full-Duplex Barge-In Interrupt: speaking while Sirius is SPEAKING or EXECUTING
            if (
                self._state in (AgentState.SPEAKING, AgentState.THINKING, AgentState.EXECUTING)
                and self._consecutive_speech_frames >= self._settings.barge_in_min_frames
            ):
                logger.info(
                    "local_vad_barge_in_detected",
                    previous_state=self._state.value,
                    vad_prob=round(vad_prob, 3),
                )
                self.audio_io.flush_playback()
                self.local_voice.stop_speaking()
                if self._on_barge_in is not None:
                    await self._on_barge_in("local_vad_barge_in")
                await self.set_state(AgentState.LISTENING, reason="local_vad_barge_in")

            # 4. Route audio to Gemini 3.8 Live (with Hybrid VAD silence trigger) OR Offline Whisper
            if (
                not self._settings.offline_mode
                and self.live_client is not None
                and self.live_client.is_connected
            ):
                await self.live_client.send_audio_frame(frame)
                if self._consecutive_silence_frames == 18:
                    await self.live_client.send_audio_stream_end()
            elif self._state == AgentState.LISTENING:
                await self._accumulate_offline_utterance(frame, is_speech)

            await self._check_conversation_timeout()

    async def _accumulate_offline_utterance(
        self,
        frame: NDArray[np.float32],
        is_speech: bool,
    ) -> None:
        if time.monotonic() < self._wake_grace_until:
            return

        if is_speech:
            self._offline_speech_buffer.append(frame.copy())
            return

        # 18 frames * 32ms = 576ms trailing silence after >= 8 speech frames (~256ms)
        if len(self._offline_speech_buffer) >= 8 and self._consecutive_silence_frames >= 18:
            utterance = np.concatenate(self._offline_speech_buffer)
            self._offline_speech_buffer.clear()
            await self.set_state(AgentState.THINKING, reason="offline_stt_transcribing")
            transcript = await self.local_voice.transcribe_utterance(utterance)
            if not transcript:
                await self.set_state(AgentState.LISTENING, reason="empty_offline_utterance")
                return

            await self._ipc.broadcast(TranscriptMessage(role="user", text=transcript, final=True))
            spot = await self.handle_spoken_or_typed_phrase(transcript)
            if spot is not None and spot.kind in ("kill_switch", "sleep_phrase"):
                return

            if self._on_offline_command is not None:
                await self.set_state(AgentState.EXECUTING, reason="offline_command_execution")
                reply = await self._on_offline_command(transcript)
                if reply:
                    await self.speak_status(reply)
            if self._state not in (AgentState.SLEEPING, AgentState.AWAITING_CONFIRMATION):
                await self.set_state(AgentState.LISTENING, reason="offline_turn_complete")

    async def _check_conversation_timeout(self) -> None:
        if self._state != AgentState.LISTENING:
            return
        elapsed = time.monotonic() - self._last_activity_ts
        if elapsed >= self._settings.conversation_timeout_seconds:
            await self.sleep(reason="conversation_idle_timeout")
