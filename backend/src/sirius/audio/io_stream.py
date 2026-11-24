"""Full-duplex audio capture and low-latency interruptible speaker output stream."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any

import numpy as np
import structlog
from numpy.typing import NDArray

from sirius.audio.aec import AcousticEchoCanceller

logger = structlog.get_logger(__name__)


class AudioIOStream:
    """Manages 16kHz mono mic input and 24kHz mono speaker output with AEC coupling."""

    def __init__(
        self,
        aec: AcousticEchoCanceller,
        mic_rate: int = 16000,
        speaker_rate: int = 24000,
        mic_frame_samples: int = 512,
        speaker_block_samples: int = 768,  # 32ms @ 24kHz
        virtual_mode: bool = False,
    ) -> None:
        self._aec = aec
        self._mic_rate = mic_rate
        self._speaker_rate = speaker_rate
        self._mic_frame_samples = mic_frame_samples
        self._speaker_block_samples = speaker_block_samples
        self._virtual_mode = virtual_mode

        self._mic_queue: asyncio.Queue[NDArray[np.float32]] = asyncio.Queue(maxsize=256)
        self._speaker_lock = threading.Lock()
        self._speaker_fifo: deque[NDArray[np.float32]] = deque()
        self._speaker_pending_offset: int = 0

        self._in_stream: Any | None = None
        self._out_stream: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._last_speaker_rms: float = 0.0
        self._last_mic_rms: float = 0.0

    @property
    def is_playing(self) -> bool:
        with self._speaker_lock:
            return len(self._speaker_fifo) > 0 or self._last_speaker_rms > 0.002

    @property
    def last_mic_rms(self) -> float:
        return self._last_mic_rms

    @property
    def last_speaker_rms(self) -> float:
        return self._last_speaker_rms

    async def start(self) -> None:
        """Open full-duplex PortAudio streams or enter virtual stream mode if headless."""
        self._loop = asyncio.get_running_loop()
        self._running = True
        if self._virtual_mode:
            logger.info("audio_io_started_virtual_mode")
            return

        try:
            import sounddevice as sd

            self._in_stream = sd.InputStream(
                samplerate=self._mic_rate,
                channels=1,
                dtype="float32",
                blocksize=self._mic_frame_samples,
                latency="low",
                callback=self._input_callback,
            )
            self._out_stream = sd.OutputStream(
                samplerate=self._speaker_rate,
                channels=1,
                dtype="float32",
                blocksize=self._speaker_block_samples,
                latency="low",
                callback=self._output_callback,
            )
            self._out_stream.start()
            self._in_stream.start()
            logger.info(
                "audio_io_started_hardware",
                mic_rate=self._mic_rate,
                speaker_rate=self._speaker_rate,
            )
        except Exception as exc:
            logger.warning(
                "audio_hardware_unavailable_falling_back_to_virtual",
                reason=str(exc),
            )
            self._virtual_mode = True

    async def stop(self) -> None:
        self._running = False
        self.flush_playback()
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        self._in_stream = None
        self._out_stream = None
        logger.info("audio_io_stopped")

    def enqueue_pcm24k_bytes(self, pcm16_le: bytes, sample_rate: int = 24000) -> None:
        """Queue 16-bit signed little-endian PCM bytes for immediate speaker playback."""
        if not pcm16_le:
            return
        samples = np.frombuffer(pcm16_le, dtype=np.int16).astype(np.float32) / 32768.0
        self.enqueue_speaker_float32(samples, sample_rate=sample_rate)

    def enqueue_speaker_float32(
        self,
        samples: NDArray[np.float32],
        sample_rate: int = 24000,
    ) -> None:
        mono = samples.astype(np.float32, copy=False).reshape(-1)
        if mono.size == 0:
            return

        if sample_rate != self._speaker_rate:
            import math

            from scipy.signal import resample_poly

            gcd = math.gcd(self._speaker_rate, sample_rate)
            mono = resample_poly(
                mono, self._speaker_rate // gcd, sample_rate // gcd
            ).astype(np.float32)

        with self._speaker_lock:
            self._speaker_fifo.append(mono)

        if self._virtual_mode:
            self._aec.feed_speaker_pcm(mono, sample_rate=self._speaker_rate)
            self._last_speaker_rms = float(np.sqrt(np.mean(mono * mono)))

    def flush_playback(self) -> None:
        """Immediately discard queued TTS audio (<5ms) on barge-in or emergency stop."""
        with self._speaker_lock:
            self._speaker_fifo.clear()
            self._speaker_pending_offset = 0
        self._last_speaker_rms = 0.0
        self._aec.reset()

    async def read_clean_mic_frame(self) -> NDArray[np.float32]:
        """Await next echo-cancelled 512-sample 16kHz float32 microphone frame."""
        raw_frame = await self._mic_queue.get()
        clean = self._aec.process_mic_frame(raw_frame)
        self._last_mic_rms = float(np.sqrt(np.mean(clean * clean)))
        return clean

    async def inject_virtual_mic_frame(self, frame: NDArray[np.float32]) -> None:
        """Inject synthetic 512-sample 16kHz float32 mic frame (for testing / virtual device)."""
        await self._mic_queue.put(frame.astype(np.float32, copy=False).reshape(-1))

    def _input_callback(
        self,
        indata: NDArray[np.float32],
        frames: int,
        _time_info: Any,
        status: Any,
    ) -> None:
        if status:
            logger.debug("mic_stream_status", status=str(status))
        if not self._running or self._loop is None:
            return
        frame_copy = indata[:, 0].copy()

        def _push() -> None:
            if self._mic_queue.full():
                try:
                    self._mic_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self._mic_queue.put_nowait(frame_copy)
            except asyncio.QueueFull:
                pass

        self._loop.call_soon_threadsafe(_push)

    def _output_callback(
        self,
        outdata: NDArray[np.float32],
        frames: int,
        _time_info: Any,
        status: Any,
    ) -> None:
        if status:
            logger.debug("speaker_stream_status", status=str(status))
        out = np.zeros(frames, dtype=np.float32)
        written = 0

        with self._speaker_lock:
            while written < frames and self._speaker_fifo:
                head = self._speaker_fifo[0]
                avail = head.size - self._speaker_pending_offset
                take = min(frames - written, avail)
                out[written : written + take] = head[
                    self._speaker_pending_offset : self._speaker_pending_offset + take
                ]
                written += take
                self._speaker_pending_offset += take
                if self._speaker_pending_offset >= head.size:
                    self._speaker_fifo.popleft()
                    self._speaker_pending_offset = 0

        outdata[:, 0] = out
        self._last_speaker_rms = float(np.sqrt(np.mean(out * out)))
        if written > 0:
            self._aec.feed_speaker_pcm(out, sample_rate=self._speaker_rate)
