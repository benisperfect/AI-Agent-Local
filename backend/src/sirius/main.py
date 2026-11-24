"""CLI & Asynchronous Daemon Entrypoint for Sirius."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys

import structlog

from sirius.agent.orchestrator import SiriusOrchestrator
from sirius.browser.profiles import discover_browser_profile
from sirius.config import SecretStore, SiriusSettings
from sirius.logging_config import configure_logging
from sirius.os_control.controller import create_os_adapter

logger = structlog.get_logger(__name__)


async def _run_doctor(settings: SiriusSettings) -> int:
    adapter = create_os_adapter()
    perms = await adapter.check_permissions()
    secrets = SecretStore(data_dir=settings.data_dir)
    profile = discover_browser_profile(settings.primary_browser)
    report = {
        "os": adapter.os_name,
        "wake_word": settings.wake_word,
        "voice_name": settings.voice_name,
        "primary_browser": {
            "kind": profile.kind,
            "display_name": profile.display_name,
            "executable_path": profile.executable_path,
            "user_data_dir": str(profile.user_data_dir),
            "supports_cdp": profile.supports_cdp,
        },
        "permissions": perms.to_bool_map(),
        "permission_details": perms.details,
        "gemini_api_key_configured": bool(secrets.get_gemini_api_key()),
        "ipc_token_path": str(settings.data_dir / "ipc.token"),
    }
    print(json.dumps(report, indent=2))
    return 0


async def run_daemon(settings: SiriusSettings, download_models: bool = False) -> None:
    settings.ensure_dirs()
    orchestrator = SiriusOrchestrator(settings=settings)

    if download_models:
        await asyncio.to_thread(orchestrator.voice.spotter.ensure_vosk_model)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop_event.set()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_stop)

    await orchestrator.start()
    try:
        await stop_event.wait()
    finally:
        await orchestrator.stop()


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sirius",
        description="Sirius: Cross-Platform Autonomous Voice-Controlled Computer & Web AI Agent",
    )
    parser.add_argument("--wake-word", help="Override custom wake word (default: 'hey sirius')")
    parser.add_argument(
        "--browser",
        choices=["brave", "chrome", "edge", "firefox"],
        help="Override primary browser (default: brave)",
    )
    parser.add_argument("--ipc-port", type=int, help="Override localhost WebSocket IPC port")
    parser.add_argument("--offline", action="store_true", help="Run strictly in local offline mode")
    parser.add_argument(
        "--download-models",
        action="store_true",
        help="Auto-download lightweight offline Vosk & Silero VAD models on startup",
    )
    parser.add_argument(
        "--set-api-key",
        metavar="KEY",
        help="Save GEMINI_API_KEY securely into OS Keychain and exit",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run cross-platform OS permissions, browser profile, and keychain check",
    )
    parser.add_argument("--json-logs", action="store_true", help="Emit JSON structured logs")
    args = parser.parse_args(argv)

    settings = SiriusSettings()
    if args.wake_word:
        settings.wake_word = args.wake_word.strip().lower()
    if args.browser:
        settings.primary_browser = args.browser
    if args.ipc_port:
        settings.ipc_port = args.ipc_port
    if args.offline:
        settings.offline_mode = True
    if args.json_logs:
        settings.log_json = True

    configure_logging(level=settings.log_level, json_output=settings.log_json)
    settings.ensure_dirs()

    if args.set_api_key:
        store = SecretStore(data_dir=settings.data_dir)
        store.set_secret("GEMINI_API_KEY", args.set_api_key.strip())
        print("Saved GEMINI_API_KEY securely in OS Keychain / Sirius Vault.")
        return 0

    if args.doctor:
        return asyncio.run(_run_doctor(settings))

    try:
        asyncio.run(run_daemon(settings, download_models=args.download_models))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
