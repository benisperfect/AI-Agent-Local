"""Silero VAD ONNX real-time streaming Voice Activity Detector."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import numpy as np
import onnxruntime as ort
import structlog
from numpy.typing import NDArray

logger = structlog.get_logger(__name__)

SILERO_ONNX_URL = (
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
)


class SileroVAD:
    """Streaming 16kHz 512-sample (32ms) Silero VAD ONNX detector with spectral fallback."""

    def __init__(
        self,
        models_dir: Path,
        sample_rate: int = 16000,
        threshold: float = 0.52,
        auto_download: bool = True,
    ) -> None:
        self._models_dir = models_dir
        self._sample_rate = sample_rate
        self._threshold = threshold
        self._model_path = models_dir / "silero_vad.onnx"
        self._session: Any | None = None

        # Silero v5 recurrent state [2, 1, 128] + 64-sample context
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)
        self._noise_floor_rms: float = 0.004

        if auto_download and not self._model_path.exists():
            self._try_download_model()

        if self._model_path.exists():
            self._load_onnx_session()

    @property
    def using_onnx(self) -> bool:
        return self._session is not None

    @property
    def threshold(self) -> float:
        return self._threshold

    def _try_download_model(self) -> None:
        self._models_dir.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.Client(follow_redirects=True, timeout=8.0) as client:
                resp = client.get(SILERO_ONNX_URL)
                resp.raise_for_status()
                self._model_path.write_bytes(resp.content)
                logger.info("silero_vad_downloaded", path=str(self._model_path))
        except Exception as exc:
            logger.warning("silero_vad_download_skipped_using_spectral_vad", reason=str(exc))

    def _load_onnx_session(self) -> None:
        try:
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(self._model_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            logger.info("silero_vad_onnx_ready", path=str(self._model_path))
        except Exception as exc:
            logger.warning("silero_vad_onnx_load_failed", error=str(exc))
            self._session = None

    def reset_states(self) -> None:
        """Reset recurrent LSTM/state tensors between utterances."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)

    def speech_probability(self, frame: NDArray[np.float32]) -> float:
        """Compute speech probability [0.0, 1.0] for a 512-sample 16kHz float32 frame."""
        mono = frame.astype(np.float32, copy=False).reshape(-1)
        if mono.size == 0:
            return 0.0

        if mono.size < 512:
            padded = np.zeros(512, dtype=np.float32)
            padded[: mono.size] = mono
            mono = padded
        elif mono.size > 512:
            mono = mono[:512]

        if self._session is not None:
            try:
                x = mono.reshape(1, 512)
                x_with_ctx = np.concatenate([self._context, x], axis=1)
                ort_inputs: dict[str, Any] = {
                    "input": x_with_ctx,
                    "state": self._state,
                    "sr": np.array(self._sample_rate, dtype=np.int64),
                }
                out, new_state = self._session.run(None, ort_inputs)
                self._state = np.asarray(new_state, dtype=np.float32)
                self._context = x_with_ctx[:, -64:]
                prob = float(np.asarray(out).reshape(-1)[0])
                return max(0.0, min(1.0, prob))
            except Exception as exc:
                logger.warning("silero_vad_inference_fallback", error=str(exc))
                self._session = None

        return self._spectral_speech_probability(mono)

    def is_speech(self, frame: NDArray[np.float32]) -> tuple[bool, float]:
        """Return (is_speech_bool, speech_probability)."""
        prob = self.speech_probability(frame)
        return prob >= self._threshold, prob

    def _spectral_speech_probability(self, mono: NDArray[np.float32]) -> float:
        """Calibrated offline voice-band (250Hz-3600Hz) spectral flatness + SNR detector."""
        rms = float(np.sqrt(np.mean(mono * mono)) + 1e-9)
        windowed = mono * np.hanning(mono.size).astype(np.float32)
        mag = np.abs(np.fft.rfft(windowed)) + 1e-9
        freqs = np.fft.rfftfreq(mono.size, d=1.0 / self._sample_rate)

        voice_mask = (freqs >= 250.0) & (freqs <= 3600.0)
        voice_power = float(np.sum(mag[voice_mask] ** 2))
        total_power = float(np.sum(mag**2)) + 1e-9
        voice_ratio = voice_power / total_power

        # Spectral flatness (Wiener entropy): speech has tonal formants (<0.45), white noise ~ 1.0
        geom_mean = float(np.exp(np.mean(np.log(mag[voice_mask]))))
        arith_mean = float(np.mean(mag[voice_mask])) + 1e-9
        flatness = geom_mean / arith_mean

        if rms < self._noise_floor_rms * 1.6:
            self._noise_floor_rms = 0.98 * self._noise_floor_rms + 0.02 * rms

        snr = rms / max(self._noise_floor_rms, 1e-5)
        raw_score = 0.0
        if snr > 2.2:
            snr_term = min((snr - 1.8) / 8.0, 1.0)
            tonality_term = max(0.0, min(1.0, 1.15 - flatness))
            band_term = max(0.0, min(1.0, voice_ratio * 1.35))
            raw_score = 0.50 * snr_term + 0.25 * tonality_term + 0.25 * band_term

        return float(max(0.0, min(1.0, raw_score)))
