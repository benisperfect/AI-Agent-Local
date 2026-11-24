"""Offline Edge STT (`faster-whisper`) & TTS (`Kokoro`/`Piper`/Native OS) Fallback."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from numpy.typing import NDArray

from sirius.audio.io_stream import AudioIOStream

logger = structlog.get_logger(__name__)


class LocalVoiceEngine:
    """Offline Speech-to-Text and Text-to-Speech fallback for local OS commands."""

    def __init__(
        self,
        models_dir: Path,
        whisper_model_size: str = "base.en",
        audio_io: AudioIOStream | None = None,
    ) -> None:
        self._models_dir = models_dir
        self._whisper_model_size = whisper_model_size
        self._audio_io = audio_io
        self._whisper: Any | None = None
        self._kokoro: Any | None = None
        self._active_tts_proc: asyncio.subprocess.Process | None = None

    def _ensure_whisper(self) -> Any:
        if self._whisper is None:
            from faster_whisper import WhisperModel

            cache_root = self._models_dir / "whisper"
            cache_root.mkdir(parents=True, exist_ok=True)
            self._whisper = WhisperModel(
                self._whisper_model_size,
                device="cpu",
                compute_type="int8",
                download_root=str(cache_root),
            )
            logger.info("faster_whisper_ready", model=self._whisper_model_size)
        return self._whisper

    def _try_load_kokoro(self) -> Any | None:
        if self._kokoro is not None:
            return self._kokoro
        kokoro_onnx = self._models_dir / "kokoro-v1.0.onnx"
        voices_bin = self._models_dir / "voices-v1.0.bin"
        if not (kokoro_onnx.exists() and voices_bin.exists()):
            return None
        try:
            from kokoro_onnx import Kokoro

            self._kokoro = Kokoro(str(kokoro_onnx), str(voices_bin))
            logger.info("kokoro_onnx_tts_ready")
            return self._kokoro
        except Exception as exc:
            logger.debug("kokoro_unavailable", reason=str(exc))
            return None

    async def transcribe_utterance(self, audio_16k: NDArray[np.float32]) -> str:
        """Transcribe a 16kHz float32 utterance locally using CTranslate2 faster-whisper."""
        mono = audio_16k.astype(np.float32, copy=False).reshape(-1)
        if mono.size < 1600:  # <100ms
            return ""

        def _run_sync() -> str:
            model = self._ensure_whisper()
            segments, _info = model.transcribe(
                mono,
                language="en",
                beam_size=2,
                vad_filter=True,
                without_timestamps=True,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()

        return await asyncio.to_thread(_run_sync)

    async def speak(self, text: str) -> None:
        """Synthesize and play spoken response locally; immediately cancellable on barge-in."""
        clean = text.strip()
        if not clean:
            return

        # Tier 1 Offline TTS: Kokoro ONNX neural voice -> AudioIOStream (AEC-coupled!)
        kokoro = self._try_load_kokoro()
        if kokoro is not None and self._audio_io is not None:
            samples, sr = await asyncio.to_thread(
                kokoro.create, clean, voice="af_heart", speed=1.02, lang="en-us"
            )
            self._audio_io.enqueue_speaker_float32(
                np.asarray(samples, dtype=np.float32), sample_rate=int(sr)
            )
            return

        # Tier 2 Offline TTS: Piper CLI streaming raw 22050Hz PCM -> AudioIOStream
        piper_bin = shutil.which("piper")
        piper_model = next(self._models_dir.glob("*.onnx.json"), None)
        if piper_bin and piper_model and self._audio_io is not None:
            onnx_file = str(piper_model).removesuffix(".json")
            proc = await asyncio.create_subprocess_exec(
                piper_bin,
                "--model",
                onnx_file,
                "--output-raw",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._active_tts_proc = proc
            stdout_bytes, _ = await proc.communicate(clean.encode("utf-8"))
            self._active_tts_proc = None
            if stdout_bytes:
                pcm_f32 = np.frombuffer(stdout_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                self._audio_io.enqueue_speaker_float32(pcm_f32, sample_rate=22050)
            return

        # Tier 3 Offline TTS: Zero-dependency OS native speech engine
        cmd = self._native_os_tts_command(clean)
        if cmd is None:
            logger.info("local_tts_console_fallback", spoken_text=clean)
            return

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._active_tts_proc = proc
        try:
            await proc.wait()
        finally:
            self._active_tts_proc = None

    def stop_speaking(self) -> None:
        """Halt active local TTS immediately on barge-in or emergency stop."""
        if self._audio_io is not None:
            self._audio_io.flush_playback()
        proc = self._active_tts_proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        self._active_tts_proc = None

    @staticmethod
    def _native_os_tts_command(text: str) -> list[str] | None:
        if sys.platform == "darwin" and shutil.which("say"):
            return ["say", "-r", "185", text]
        if sys.platform == "win32":
            escaped = text.replace("'", "''")
            ps_script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Rate = 1; "
                f"$s.Speak('{escaped}')"
            )
            return ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script]
        for linux_bin, args in (
            ("espeak-ng", ["-s", "175", text]),
            ("spd-say", ["--wait", text]),
        ):
            resolved = shutil.which(linux_bin)
            if resolved:
                return [resolved, *args]
        return None
