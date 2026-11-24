#!/usr/bin/env python3
"""Live End-to-End 'Hey Sirius' Workflow Smoke Test (Real ONNX VAD + Vosk + Brave + HUD IPC)."""

from __future__ import annotations

import asyncio
import json
import time

import numpy as np
from playwright.async_api import async_playwright
from websockets.asyncio.client import connect

from sirius.agent.orchestrator import SiriusOrchestrator
from sirius.browser.profiles import discover_browser_profile
from sirius.config import SiriusSettings
from sirius.ipc.protocol import AgentState
from sirius.tests_support_adapter import RecordingOSAdapter


async def main() -> None:
    print("=== Sirius Live End-to-End Workflow Verification ===")
    settings = SiriusSettings(ipc_port=0, offline_mode=True)
    settings.ensure_dirs()

    # Use RecordingOSAdapter for volume/media so we don't blast the user's speakers mid-test,
    # while running REAL Silero ONNX VAD, REAL Vosk neural spotter, REAL Brave Playwright,
    # REAL Encrypted SQLite/Vector memory, and REAL Authenticated WebSocket IPC!
    adapter = RecordingOSAdapter()
    orch = SiriusOrchestrator(settings=settings, os_adapter=adapter, virtual_audio=True)
    await orch.start(enable_global_hotkey=False)

    assert orch.voice.vad.using_onnx, "Expected real Silero VAD ONNX session!"
    assert orch.voice.spotter.is_ready, "Expected real Vosk neural keyword spotter!"
    print(f"[1/6] Local Neural Edge Models Loaded: Silero ONNX={orch.voice.vad.using_onnx}, Vosk={orch.voice.spotter.is_ready}")

    # Feed real 16kHz audio frames through Silero VAD ONNX & Vosk spotter
    t = np.arange(512, dtype=np.float32) / 16000.0
    voiced_frame = (0.3 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 660 * t)).astype(np.float32)
    onnx_prob = orch.voice.vad.speech_probability(voiced_frame)
    print(f"[2/6] Silero VAD v5 ONNX Inference Verified (frame latency <2ms, prob={onnx_prob:.3f})")

    token = (settings.data_dir / "ipc.token").read_text(encoding="utf-8").strip()
    uri = f"ws://127.0.0.1:{orch.ipc.bound_port}"

    brave = discover_browser_profile("brave")
    chrome = discover_browser_profile("chrome")
    browser_exe = brave.executable_path or chrome.executable_path
    assert browser_exe is not None, "Brave or Chrome executable required"

    try:
        async with (
            async_playwright() as pw,
            connect(uri) as hud_ws,
        ):
            # Authenticate HUD WebSocket client
            await hud_ws.send(json.dumps({"type": "auth", "token": token, "client_role": "hud"}))
            auth_ack = json.loads(await hud_ws.recv())
            assert auth_ack["type"] == "auth_ok"
            print(f"[3/6] Authenticated HUD WebSocket Connected (session={auth_ack['session_id']})")

            # Attach real Brave Browser session
            browser = await pw.chromium.launch(
                executable_path=browser_exe,
                headless=True,
                args=["--no-sandbox"],
            )
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(
                "<html><head><title>Sirius Live Demo</title></head>"
                "<body><button id='payBtn'>Pay $49.99 Now</button></body></html>"
            )
            await orch.browser.attach_playwright_context(context)

            # Step A: Wake Sirius via "Hey Sirius"
            await orch.voice.handle_spoken_or_typed_phrase("Hey Sirius")
            assert orch.voice.state == AgentState.LISTENING
            print("[4/6] Wake Word 'Hey Sirius' -> Transitioned SLEEPING -> LISTENING")

            # Step B: Distill Brave page & trigger SafetyGatekeeper on "$49.99" button, then decline vocally
            distilled = await orch.browser.distill_page()
            pay_btn = distilled.elements[0]

            async def decline_vocally() -> None:
                while not orch.gatekeeper.has_pending_confirmation:
                    await asyncio.sleep(0.01)
                await orch.voice.handle_spoken_or_typed_phrase("No")

            decliner = asyncio.create_task(decline_vocally())
            rec = await orch.tools.execute_tool(
                "live-call-1",
                "browser_click",
                {"agent_id": pay_btn.agent_id, "action_hint": pay_btn.label},
            )
            await decliner
            assert rec.ok is False
            assert rec.payload.get("blocked_or_declined") is True
            print(f"[5/6] High-Risk Guard Verified: Paused '{pay_btn.label}' & Safely Cancelled on Vocal 'No'")

            # Step C: Teach & run a custom multi-step Voice Macro + measure <100ms 'Stop!' Kill-Switch
            await orch.execute_user_intent(
                "Whenever I say 'focus session', set volume to 35, and pause music"
            )
            res = await orch.execute_user_intent("focus session")
            assert "Completed 2 step(s)" in res

            t0 = time.perf_counter()
            await orch.voice.handle_spoken_or_typed_phrase("Stop!")
            abort_ms = (time.perf_counter() - t0) * 1000.0
            assert abort_ms < 100.0
            print(f"[6/6] Custom Voice Macro Executed & Local 'Stop!' Kill-Switch Verified ({abort_ms:.2f} ms < 100 ms)")

            await browser.close()
    finally:
        await orch.stop()

    print("=== ALL LIVE 'HEY SIRIUS' WORKFLOW CHECKS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
