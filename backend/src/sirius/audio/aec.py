"""Frequency-Domain Adaptive Filter (FDAF/NLMS) Acoustic Echo Cancellation."""

from __future__ import annotations

import math
import threading
from collections import deque

import numpy as np
from numpy.typing import NDArray
from scipy.signal import resample_poly


class AcousticEchoCanceller:
    """Real-time full-duplex Acoustic Echo Canceller with Double-Talk Detection.

    Cancels 24kHz/16kHz speaker reference playback from incoming 16kHz microphone
    frames using overlap-save Frequency-Domain Normalized Least Mean Squares (FD-NLMS)
    and residual echo spectral suppression so Sirius's own TTS never re-triggers VAD.
    """

    def __init__(
        self,
        frame_size: int = 512,
        filter_blocks: int = 8,
        step_size: float = 0.35,
        mic_rate: int = 16000,
        speaker_rate: int = 24000,
    ) -> None:
        self._frame_size = frame_size
        self._fft_size = frame_size * 2
        self._num_bins = self._fft_size // 2 + 1
        self._num_blocks = filter_blocks
        self._mu = step_size
        self._mic_rate = mic_rate
        self._speaker_rate = speaker_rate
        self._eps = 1e-8

        self._lock = threading.Lock()
        self._ref_fifo: deque[float] = deque(maxlen=mic_rate * 4)

        # Multi-delay frequency-domain filter weights [num_blocks, num_bins]
        self._weights = np.zeros((self._num_blocks, self._num_bins), dtype=np.complex128)
        self._ref_spectra = np.zeros((self._num_blocks, self._num_bins), dtype=np.complex128)
        self._prev_ref_frame = np.zeros(self._frame_size, dtype=np.float64)
        self._power_est = np.full(self._num_bins, 1e-4, dtype=np.float64)
        self._last_attenuation_db: float = 0.0

    @property
    def last_attenuation_db(self) -> float:
        return self._last_attenuation_db

    def reset(self) -> None:
        """Clear adaptive filter buffers on playback flush."""
        with self._lock:
            self._ref_fifo.clear()
            self._ref_spectra.fill(0.0)
            self._prev_ref_frame.fill(0.0)
            self._last_attenuation_db = 0.0

    def feed_speaker_pcm(self, speaker_samples: NDArray[np.float32], sample_rate: int | None = None) -> None:
        """Feed outgoing speaker samples into the far-end reference delay line."""
        rate = sample_rate or self._speaker_rate
        mono = speaker_samples.astype(np.float64, copy=False).reshape(-1)
        if mono.size == 0:
            return

        if rate != self._mic_rate:
            gcd = math.gcd(self._mic_rate, rate)
            up = self._mic_rate // gcd
            down = rate // gcd
            resampled = resample_poly(mono, up, down)
        else:
            resampled = mono

        with self._lock:
            self._ref_fifo.extend(float(x) for x in resampled)

    def process_mic_frame(self, mic_frame: NDArray[np.float32]) -> NDArray[np.float32]:
        """Subtract estimated speaker echo from a 16kHz mono float32 mic frame."""
        d_frame = mic_frame.astype(np.float64, copy=False).reshape(-1)
        n = d_frame.size
        if n != self._frame_size:
            return mic_frame

        with self._lock:
            if len(self._ref_fifo) >= n:
                x_frame = np.fromiter(
                    (self._ref_fifo.popleft() for _ in range(n)),
                    dtype=np.float64,
                    count=n,
                )
            elif len(self._ref_fifo) > 0:
                avail = len(self._ref_fifo)
                x_frame = np.zeros(n, dtype=np.float64)
                for idx in range(avail):
                    x_frame[idx] = self._ref_fifo.popleft()
            else:
                self._last_attenuation_db = 0.0
                return mic_frame

            ref_energy = float(np.dot(x_frame, x_frame)) / n
            if ref_energy < 1e-7:
                self._prev_ref_frame = x_frame
                self._last_attenuation_db = 0.0
                return mic_frame

            # Overlap-save 2N block [prev_ref, current_ref]
            x_block = np.concatenate([self._prev_ref_frame, x_frame])
            self._prev_ref_frame = x_frame.copy()

            x_spec = np.fft.rfft(x_block)
            self._ref_spectra = np.roll(self._ref_spectra, shift=1, axis=0)
            self._ref_spectra[0] = x_spec

            # Predicted echo in time domain (second half of IFFT is linear convolution)
            y_spec = np.sum(self._weights * self._ref_spectra, axis=0)
            y_full = np.fft.irfft(y_spec, n=self._fft_size)
            y_hat = y_full[self._frame_size :]

            e_frame = d_frame - y_hat

            mic_energy = float(np.dot(d_frame, d_frame)) / n + self._eps
            err_energy = float(np.dot(e_frame, e_frame)) / n + self._eps

            # Geigel / Energy-ratio Double-Talk Detector: freeze adaptation during user barge-in
            double_talk = err_energy > (1.35 * ref_energy) and mic_energy > (1.8 * ref_energy)

            if not double_talk:
                e_padded = np.concatenate([np.zeros(self._frame_size, dtype=np.float64), e_frame])
                e_spec = np.fft.rfft(e_padded)
                self._power_est = 0.9 * self._power_est + 0.1 * (np.abs(x_spec) ** 2)
                norm = np.sum(np.abs(self._ref_spectra) ** 2, axis=0) + self._power_est + 1e-4

                for b_idx in range(self._num_blocks):
                    grad_freq = np.conj(self._ref_spectra[b_idx]) * e_spec / norm
                    # Constrain gradient to causal half (first N samples)
                    grad_time = np.fft.irfft(grad_freq, n=self._fft_size)
                    grad_time[self._frame_size :] = 0.0
                    self._weights[b_idx] += self._mu * np.fft.rfft(grad_time)

                # Mild non-linear residual echo suppression when user is not speaking
                e_fft = np.fft.rfft(e_frame)
                yhat_fft = np.fft.rfft(y_hat)
                gain = np.clip(
                    1.0 - 0.55 * np.abs(yhat_fft) / (np.abs(e_fft) + 1e-6),
                    0.12,
                    1.0,
                )
                e_frame = np.fft.irfft(e_fft * gain, n=n)

            atten = 10.0 * math.log10(max(mic_energy / err_energy, 1.0))
            self._last_attenuation_db = round(min(atten, 40.0), 2)
            return np.clip(e_frame, -1.0, 1.0).astype(np.float32)
