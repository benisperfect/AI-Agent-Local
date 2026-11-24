"""Phase 5 Backend Verification Suite: Encrypted SQLite/Vector Memory, Voice Macros & Full Orchestrator E2E."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet
from websockets.asyncio.client import connect

from sirius.agent.orchestrator import SiriusOrchestrator
from sirius.config import SiriusSettings
from sirius.main import cli_main
from sirius.memory.macros import VoiceMacroRegistry, parse_spoken_macro_definition
from sirius.memory.store import EncryptedMemoryStore
from sirius.tests_support_adapter import RecordingOSAdapter


async def test_encrypted_sqlite_memory_and_semantic_vector_search(tmp_path: Path) -> None:
    db_file = tmp_path / "memory.sqlite3"
    key = Fernet.generate_key()
    store = EncryptedMemoryStore(db_path=db_file, fernet_key=key)

    await store.upsert_memory("contacts", "my accountant", "cpa@ledgerfirm.com")
    await store.upsert_memory("folders", "taxes spreadsheet", "/home/ben/Documents/Taxes_2026.xlsx")

    # Verify ciphertext encryption at rest in SQLite (plaintext email must NOT appear in raw DB row!)
    conn = sqlite3.connect(str(db_file))
    raw_blob = conn.execute("SELECT encrypted_value FROM memories LIMIT 1").fetchone()[0]
    conn.close()
    assert b"cpa@ledgerfirm.com" not in bytes(raw_blob)

    # Semantic search retrieves relevant contact alias
    hits = await store.search_semantic("message my accountant about taxes")
    assert len(hits) >= 1
    assert any(h.value == "cpa@ledgerfirm.com" for h in hits)

    # Spoken macro definition parser & storage
    parsed = parse_spoken_macro_definition(
        "Whenever I say 'start focus mode', close Slack, set volume to 25, and pause music"
    )
    assert parsed is not None
    trigger, steps = parsed
    assert trigger == "start focus mode"
    assert len(steps) == 3

    reg = VoiceMacroRegistry(store)
    learned = await reg.try_learn_from_utterance(
        "Whenever I say 'start focus mode', close Slack, set volume to 25, and pause music"
    )
    assert learned is not None
    matched = await reg.match_utterance("Hey Sirius, start focus mode please")
    assert matched is not None
    assert matched.trigger_phrase == "start focus mode"


async def test_full_sirius_orchestrator_macro_execution_and_hud_ipc(tmp_path: Path) -> None:
    settings = SiriusSettings(
        data_dir=tmp_path / "sirius_e2e",
        ipc_port=0,
        offline_mode=True,
    )
    rec_adapter = RecordingOSAdapter()
    orch = SiriusOrchestrator(settings=settings, os_adapter=rec_adapter, virtual_audio=True)
    await orch.start(enable_global_hotkey=False)

    token = (settings.data_dir / "ipc.token").read_text(encoding="utf-8")
    uri = f"ws://127.0.0.1:{orch.ipc.bound_port}"

    try:
        # 1. Teach Sirius a custom voice macro through natural speech/text
        ack = await orch.execute_user_intent(
            "Whenever I say 'start focus mode', close Slack, set volume to 30, and pause music"
        )
        assert "start focus mode" in ack

        # 2. Trigger the learned macro and verify all 3 Tier-1 OS actions ran deterministically!
        summary = await orch.execute_user_intent("start focus mode")
        assert "Completed 3 step(s)" in summary
        assert ("close_window", "slack") in rec_adapter.calls
        assert ("set_volume", 30) in rec_adapter.calls
        assert ("media_control", "play_pause") in rec_adapter.calls

        # 3. Connect simulated Tauri v2 HUD client over authenticated WebSocket & verify snapshot
        async with connect(uri) as hud_ws:
            await hud_ws.send(
                json.dumps({"type": "auth", "token": token, "client_role": "hud"})
            )
            first = json.loads(await hud_ws.recv())
            assert first["type"] == "auth_ok"

            await hud_ws.send(json.dumps({"type": "request_snapshot"}))
            msg_a = json.loads(await hud_ws.recv())
            msg_b = json.loads(await hud_ws.recv())
            types_seen = {msg_a["type"], msg_b["type"]}
            assert "system_status" in types_seen
            assert "memory_snapshot" in types_seen
    finally:
        await orch.stop()


def test_cli_doctor_command() -> None:
    exit_code = cli_main(["--doctor"])
    assert exit_code == 0
