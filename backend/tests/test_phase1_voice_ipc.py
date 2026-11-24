"""Phase 1 Verification Suite: Core Daemon, Authenticated IPC & Full-Duplex Voice Pipeline."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import numpy as np
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from sirius.audio.aec import AcousticEchoCanceller
from sirius.audio.pipeline import VoicePipeline
from sirius.audio.vad import SileroVAD
from sirius.audio.wake_kill import LocalWakeAndKillDetector
from sirius.config import SecretStore, SiriusSettings
from sirius.ipc.protocol import AgentState, TranscriptMessage
from sirius.ipc.server import IPCServer


def _make_speech_like_signal(num_samples: int, sample_rate: int = 16000) -> np.ndarray:
    """Synthesize a voiced multi-harmonic speech-band signal (F0=165Hz + formants)."""
    t = np.arange(num_samples, dtype=np.float32) / float(sample_rate)
    sig = (
        0.35 * np.sin(2.0 * np.pi * 320.0 * t)
        + 0.25 * np.sin(2.0 * np.pi * 640.0 * t)
        + 0.18 * np.sin(2.0 * np.pi * 1280.0 * t)
        + 0.10 * np.sin(2.0 * np.pi * 2150.0 * t)
    )
    return sig.astype(np.float32)


def test_settings_and_secret_store(tmp_path: Path) -> None:
    settings = SiriusSettings(data_dir=tmp_path / "sirius_data")
    settings.ensure_dirs()
    assert settings.wake_word == "hey sirius"
    assert settings.primary_browser == "brave"
    assert settings.voice_name == "Aoede"
    assert settings.gemini_live_model == "gemini-3.8-live"
    assert settings.gemini_vision_model == "gemini-4-hydrogen"
    assert settings.gemini_embedding_model == "gemini-embedding-2"

    store = SecretStore(data_dir=settings.data_dir)
    token1 = store.get_or_create_ipc_token()
    token2 = store.get_or_create_ipc_token()
    assert len(token1) >= 32
    assert token1 == token2
    assert (settings.data_dir / "ipc.token").read_text(encoding="utf-8") == token1


async def test_ipc_auth_broadcast_and_extension_rpc() -> None:
    received_cmds: list[str] = []

    async def on_msg(msg: object, _sid: str) -> None:
        received_cmds.append(getattr(msg, "type", "unknown"))

    ipc = IPCServer(host="127.0.0.1", port=0, auth_token="secret-token-123", on_message=on_msg)
    await ipc.start()
    uri = f"ws://127.0.0.1:{ipc.bound_port}"

    try:
        # 1. Invalid token must be rejected with 4403
        async with connect(uri) as bad_ws:
            await bad_ws.send(
                json.dumps({"type": "auth", "token": "wrong-token", "client_role": "hud"})
            )
            err_raw = await bad_ws.recv()
            err = json.loads(err_raw)
            assert err["type"] == "error"
            assert err["code"] == "INVALID_TOKEN"
            with pytest.raises(ConnectionClosed):
                await bad_ws.recv()

        # 2. Valid HUD client + Browser Extension client RPC round-trip
        async with connect(uri) as hud_ws, connect(uri) as ext_ws:
            await hud_ws.send(
                json.dumps({"type": "auth", "token": "secret-token-123", "client_role": "hud"})
            )
            hud_ack = json.loads(await hud_ws.recv())
            assert hud_ack["type"] == "auth_ok"

            await ext_ws.send(
                json.dumps(
                    {
                        "type": "auth",
                        "token": "secret-token-123",
                        "client_role": "browser_extension",
                    }
                )
            )
            ext_ack = json.loads(await ext_ws.recv())
            assert ext_ack["type"] == "auth_ok"
            assert ipc.has_extension_client

            # Broadcast to HUD
            await ipc.broadcast(
                TranscriptMessage(role="assistant", text="Good morning.", final=True),
                roles={"hud"},
            )
            broadcast_msg = json.loads(await hud_ws.recv())
            assert broadcast_msg["type"] == "transcript"
            assert broadcast_msg["text"] == "Good morning."

            # Extension RPC responder task
            async def ext_responder() -> None:
                cmd_raw = await ext_ws.recv()
                cmd = json.loads(cmd_raw)
                assert cmd["type"] == "extension_command"
                await ext_ws.send(
                    json.dumps(
                        {
                            "type": "extension_result",
                            "request_id": cmd["request_id"],
                            "ok": True,
                            "data": {"active_url": "https://brave.com"},
                        }
                    )
                )

            resp_task = asyncio.create_task(ext_responder())
            rpc_data = await ipc.call_extension("get_active_tab", {})
            await resp_task
            assert rpc_data["active_url"] == "https://brave.com"

            # Client command to daemon
            await hud_ws.send(json.dumps({"type": "wake"}))
            await asyncio.sleep(0.05)
            assert "wake" in received_cmds
    finally:
        await ipc.stop()


def test_aec_cancels_far_end_speaker_echo() -> None:
    aec = AcousticEchoCanceller(frame_size=512, mic_rate=16000, speaker_rate=16000)
    far_end = _make_speech_like_signal(512 * 24, sample_rate=16000)

    # Simulate acoustic path (gain 0.65) without near-end talker
    attenuations: list[float] = []
    for idx in range(24):
        ref_chunk = far_end[idx * 512 : (idx + 1) * 512]
        aec.feed_speaker_pcm(ref_chunk, sample_rate=16000)
        mic_echo = (0.65 * ref_chunk).astype(np.float32)
        cleaned = aec.process_mic_frame(mic_echo)
        assert cleaned.shape == (512,)
        attenuations.append(aec.last_attenuation_db)

    # Adaptive filter converges to substantial echo suppression
    assert max(attenuations[-6:]) > 5.0


def test_vad_and_local_wake_kill_spotter(tmp_path: Path) -> None:
    vad = SileroVAD(models_dir=tmp_path, auto_download=False)
    silence = np.zeros(512, dtype=np.float32)
    is_sp_sil, prob_sil = vad.is_speech(silence)
    assert not is_sp_sil
    assert prob_sil < 0.2

    speech_frame = _make_speech_like_signal(512)
    is_sp_voice, prob_voice = vad.is_speech(speech_frame)
    assert is_sp_voice
    assert prob_voice >= vad.threshold

    spotter = LocalWakeAndKillDetector(
        models_dir=tmp_path,
        wake_phrases=["hey sirius", "sirius"],
        kill_words=["stop", "cancel", "freeze"],
        sleep_phrases=["go to sleep"],
        auto_download=False,
        cooldown_seconds=0.0,
    )
    ev_wake = spotter.classify_phrase("Hey Sirius")
    assert ev_wake is not None and ev_wake.kind == "wake_word"

    # Kill-switch takes top priority even in mixed utterance
    ev_kill = spotter.classify_phrase("Sirius stop right now!")
    assert ev_kill is not None and ev_kill.kind == "kill_switch"

    ev_yes = spotter.classify_phrase("Yes, go ahead")
    assert ev_yes is not None and ev_yes.kind == "confirm_yes"


async def test_voice_pipeline_barge_in_and_sub100ms_kill_switch(tmp_path: Path) -> None:
    settings = SiriusSettings(
        data_dir=tmp_path / "data",
        offline_mode=True,
        conversation_timeout_seconds=0.35,
    )
    settings.ensure_dirs()
    ipc = IPCServer(host="127.0.0.1", port=0, auth_token="tok")
    await ipc.start()

    barge_in_events: list[str] = []
    kill_events: list[tuple[str, float]] = []

    async def on_barge_in(reason: str) -> None:
        barge_in_events.append(reason)

    async def on_kill(source: str) -> None:
        kill_events.append((source, time.monotonic()))

    pipeline = VoicePipeline(
        settings=settings,
        ipc=ipc,
        on_barge_in=on_barge_in,
        on_emergency_stop=on_kill,
        virtual_audio=True,
    )
    await pipeline.start(enable_global_hotkey=False)

    try:
        assert pipeline.state == AgentState.SLEEPING

        # 1. Wake word transitions SLEEPING -> LISTENING
        await pipeline.handle_spoken_or_typed_phrase("Hey Sirius")
        assert pipeline.state == AgentState.LISTENING

        # 2. Enter SPEAKING with queued speaker audio, then inject 4 frames (128ms) of user speech
        await pipeline.set_state(AgentState.SPEAKING, reason="test_speaking")
        pipeline.audio_io.enqueue_speaker_float32(np.ones(4800, dtype=np.float32) * 0.2)
        speech_chunk = _make_speech_like_signal(512)
        for _ in range(4):
            await pipeline.audio_io.inject_virtual_mic_frame(speech_chunk)

        await asyncio.sleep(0.08)
        assert pipeline.state == AgentState.LISTENING
        assert len(barge_in_events) >= 1
        assert not pipeline.audio_io.is_playing

        # 3. Local "Stop!" kill-switch aborts in <100ms
        await pipeline.set_state(AgentState.EXECUTING, reason="test_exec")
        t0 = time.monotonic()
        await pipeline.handle_spoken_or_typed_phrase("Stop!")
        assert len(kill_events) == 1
        latency_ms = (kill_events[0][1] - t0) * 1000.0
        assert latency_ms < 100.0
        assert pipeline.state == AgentState.LISTENING

        # 4. Conversation idle timeout returns to SLEEPING
        await asyncio.sleep(0.45)
        await pipeline.audio_io.inject_virtual_mic_frame(np.zeros(512, dtype=np.float32))
        await asyncio.sleep(0.08)
        assert pipeline.state == AgentState.SLEEPING
    finally:
        await pipeline.stop()
        await ipc.stop()
