"""Local Wake-Word ('Hey Sirius') & <100ms Emergency Kill-Switch Spotter + Global Hotkey."""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import numpy as np
import structlog
from numpy.typing import NDArray

logger = structlog.get_logger(__name__)

VOSK_SMALL_MODEL_URL = (
    "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
)
VOSK_MODEL_DIRNAME = "vosk-model-small-en-us-0.15"

SpotKind = Literal[
    "wake_word",
    "kill_switch",
    "sleep_phrase",
    "confirm_yes",
    "confirm_no",
]


@dataclass(frozen=True)
class SpotEvent:
    kind: SpotKind
    phrase: str
    timestamp_monotonic: float


class LocalWakeAndKillDetector:
    """Sub-100ms local constrained-grammar spotter for wake words, kill words, and confirmations."""

    CONFIRM_YES_WORDS: tuple[str, ...] = ("yes", "go ahead", "proceed", "confirm", "do it")
    CONFIRM_NO_WORDS: tuple[str, ...] = ("no", "do not", "abort", "never mind")

    def __init__(
        self,
        models_dir: Path,
        wake_phrases: list[str],
        kill_words: list[str],
        sleep_phrases: list[str],
        sample_rate: int = 16000,
        auto_download: bool = False,
        cooldown_seconds: float = 0.75,
    ) -> None:
        self._models_dir = models_dir
        self._sample_rate = sample_rate
        self._cooldown_seconds = cooldown_seconds
        self._last_trigger_ts: dict[SpotKind, float] = {}

        self._wake_phrases = [p.strip().lower() for p in wake_phrases if p.strip()]
        self._kill_words = [p.strip().lower() for p in kill_words if p.strip()]
        self._sleep_phrases = [p.strip().lower() for p in sleep_phrases if p.strip()]

        self._vosk_model: Any | None = None
        self._recognizer: Any | None = None

        if auto_download:
            self.ensure_vosk_model()
        else:
            model_dir = self._models_dir / VOSK_MODEL_DIRNAME
            if model_dir.exists():
                self._init_vosk(model_dir)

    @property
    def is_ready(self) -> bool:
        return self._recognizer is not None

    def ensure_vosk_model(self) -> bool:
        """Download and initialize the 40MB offline Vosk English model if not yet cached."""
        model_dir = self._models_dir / VOSK_MODEL_DIRNAME
        if not model_dir.exists():
            self._models_dir.mkdir(parents=True, exist_ok=True)
            try:
                logger.info("downloading_local_vosk_spotter_model", url=VOSK_SMALL_MODEL_URL)
                with httpx.Client(follow_redirects=True, timeout=45.0) as client:
                    resp = client.get(VOSK_SMALL_MODEL_URL)
                    resp.raise_for_status()
                    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                        zf.extractall(self._models_dir)
                logger.info("vosk_spotter_model_ready", path=str(model_dir))
            except Exception as exc:
                logger.warning("vosk_model_download_skipped", error=str(exc))
                return False

        return self._init_vosk(model_dir)

    def _build_grammar(self) -> list[str]:
        vocab = set(self._wake_phrases)
        vocab.update(self._kill_words)
        vocab.update(self._sleep_phrases)
        vocab.update(self.CONFIRM_YES_WORDS)
        vocab.update(self.CONFIRM_NO_WORDS)
        vocab.add("[unk]")
        return sorted(vocab)

    def _init_vosk(self, model_dir: Path) -> bool:
        try:
            import vosk

            vosk.SetLogLevel(-1)
            self._vosk_model = vosk.Model(str(model_dir))
            self._rebuild_recognizer()
            return True
        except Exception as exc:
            logger.warning("vosk_init_failed", error=str(exc))
            self._vosk_model = None
            self._recognizer = None
            return False

    def _rebuild_recognizer(self) -> None:
        if self._vosk_model is None:
            return
        import vosk

        grammar_json = json.dumps(self._build_grammar())
        self._recognizer = vosk.KaldiRecognizer(
            self._vosk_model, float(self._sample_rate), grammar_json
        )
        self._recognizer.SetWords(False)

    def update_wake_word(self, new_wake_word: str) -> None:
        clean = new_wake_word.strip().lower()
        if clean and clean not in self._wake_phrases:
            self._wake_phrases.insert(0, clean)
            self._rebuild_recognizer()

    def process_pcm_frame(self, frame: NDArray[np.float32]) -> SpotEvent | None:
        """Inspect a 16kHz mono float32 frame and emit immediate SpotEvent (<100ms latency)."""
        if self._recognizer is None:
            return None

        pcm16 = (np.clip(frame, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        is_final = bool(self._recognizer.AcceptWaveform(pcm16))
        if is_final:
            payload = json.loads(self._recognizer.Result())
            candidate = str(payload.get("text", "")).strip().lower()
        else:
            payload = json.loads(self._recognizer.PartialResult())
            candidate = str(payload.get("partial", "")).strip().lower()

        if not candidate or candidate == "[unk]":
            return None

        event = self.classify_phrase(candidate)
        if event is not None and not is_final:
            # Reset recognizer immediately so partial transcript does not re-emit on next 32ms frame
            self._recognizer.Reset()
        return event

    def classify_phrase(self, raw_text: str) -> SpotEvent | None:
        """Match spoken/partial text against kill-words (highest priority), wake-words, sleep, or confirmation."""
        cleaned = " ".join(
            raw_text.lower().replace("!", " ").replace(".", " ").replace(",", " ").split()
        )
        if not cleaned:
            return None

        tokens = set(cleaned.split())
        now = time.monotonic()

        # Priority 1: Emergency local kill-switch (<100ms instant abort)
        for kw in self._kill_words:
            if kw in tokens or cleaned == kw:
                return self._emit_with_cooldown("kill_switch", kw, now, cooldown_override=0.25)

        # Priority 2: Sleep phrases
        for sp in self._sleep_phrases:
            if sp in cleaned:
                return self._emit_with_cooldown("sleep_phrase", sp, now)

        # Priority 3: Wake word ("hey sirius" / "sirius")
        for wp in self._wake_phrases:
            if wp in cleaned:
                return self._emit_with_cooldown("wake_word", wp, now)

        # Priority 4: Confirmation responses (used only when gatekeeper is awaiting vocal yes/no)
        for no_w in self.CONFIRM_NO_WORDS:
            if cleaned == no_w or (no_w in tokens and " " not in no_w):
                return self._emit_with_cooldown("confirm_no", no_w, now, cooldown_override=0.4)
            if " " in no_w and no_w in cleaned:
                return self._emit_with_cooldown("confirm_no", no_w, now, cooldown_override=0.4)

        for yes_w in self.CONFIRM_YES_WORDS:
            if cleaned == yes_w or (" " not in yes_w and yes_w in tokens):
                return self._emit_with_cooldown("confirm_yes", yes_w, now, cooldown_override=0.4)
            if " " in yes_w and yes_w in cleaned:
                return self._emit_with_cooldown("confirm_yes", yes_w, now, cooldown_override=0.4)

        return None

    def _emit_with_cooldown(
        self,
        kind: SpotKind,
        phrase: str,
        now: float,
        cooldown_override: float | None = None,
    ) -> SpotEvent | None:
        cooldown = cooldown_override if cooldown_override is not None else self._cooldown_seconds
        last = self._last_trigger_ts.get(kind, -999.0)
        if (now - last) < cooldown:
            return None
        self._last_trigger_ts[kind] = now
        return SpotEvent(kind=kind, phrase=phrase, timestamp_monotonic=now)


class GlobalHotkeyKillSwitch:
    """OS-level global hotkey listener (`Ctrl+Shift+Space` / `Cmd+Shift+Space`) for instant abort."""

    def __init__(
        self,
        hotkey: str,
        on_trigger: Callable[[str], None],
    ) -> None:
        self._hotkey = hotkey
        self._on_trigger = on_trigger
        self._listener: Any | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> bool:
        """Register OS global hotkey in background thread; gracefully degrade under strict Wayland."""
        try:
            from pynput import keyboard

            def _callback() -> None:
                loop.call_soon_threadsafe(self._on_trigger, f"hotkey:{self._hotkey}")

            self._listener = keyboard.GlobalHotKeys({self._hotkey: _callback})
            self._listener.daemon = True
            self._listener.start()
            logger.info("global_kill_hotkey_registered", hotkey=self._hotkey)
            return True
        except Exception as exc:
            logger.warning(
                "global_kill_hotkey_fallback_to_hud_shortcut",
                hotkey=self._hotkey,
                reason=str(exc),
            )
            self._listener = None
            return False

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
