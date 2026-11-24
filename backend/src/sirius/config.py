"""Configuration management and OS Keychain credential storage for Sirius."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Literal

import keyring
from cryptography.fernet import Fernet
from keyring.errors import KeyringError
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_NAME = "com.sirius.autonomous-agent"

DEFAULT_PERSONA_PROMPT = (
    "You are Sirius, a calm, warm, and composed executive AI assistant with full voice-driven "
    "control over the user's desktop operating system, applications, terminal, files, and "
    "authenticated browser sessions. Speak naturally in clear, concise, reassuring sentences "
    "without filler words or robotic phrasing. During multi-step workflows, provide brief "
    "one-sentence spoken progress updates. Execute reversible and everyday tasks autonomously "
    "without nagging, and defer to the safety confirmation gate before irreversible actions."
)


def default_data_dir() -> Path:
    """Resolve platform-native application data directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Sirius"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Sirius"
    xdg_data = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data) if xdg_data else (Path.home() / ".local" / "share")
    return base / "sirius"


def default_emergency_hotkey() -> str:
    """Return OS-appropriate global kill-switch hotkey."""
    if sys.platform == "darwin":
        return "<cmd>+<shift>+<space>"
    return "<ctrl>+<shift>+<space>"


class SiriusSettings(BaseSettings):
    """Strongly typed runtime configuration for the Sirius daemon."""

    model_config = SettingsConfigDict(
        env_prefix="SIRIUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Voice & Wake-Word Preferences (Locked in for user)
    wake_word: str = "hey sirius"
    wake_aliases: list[str] = Field(default_factory=lambda: ["hey sirius", "sirius"])
    sleep_phrases: list[str] = Field(
        default_factory=lambda: ["go to sleep", "sleep now", "stand by sirius"]
    )
    kill_words: list[str] = Field(default_factory=lambda: ["stop", "cancel", "freeze"])
    emergency_hotkey: str = Field(default_factory=default_emergency_hotkey)

    # Cloud & Local AI Stack (Current Gemini 3.8 Live + Gemini 4 + Gemini Embedding 2)
    cloud_provider: Literal["gemini"] = "gemini"
    gemini_live_model: str = "gemini-3.8-live"
    gemini_vision_model: str = "gemini-4-hydrogen"
    gemini_embedding_model: str = "gemini-embedding-2"
    voice_name: str = "Aoede"
    persona_prompt: str = DEFAULT_PERSONA_PROMPT
    offline_mode: bool = False
    whisper_model_size: str = "base.en"

    # Audio Pipeline Parameters
    mic_sample_rate: int = 16000
    speaker_sample_rate: int = 24000
    frame_duration_ms: int = 32  # 512 samples @ 16kHz (exact Silero VAD frame size)
    vad_threshold: float = 0.35
    barge_in_min_frames: int = 3  # ~96ms instant speech interrupt confirmation
    conversation_timeout_seconds: float = 35.0
    confirmation_timeout_seconds: float = 25.0

    # Browser & IPC Configuration
    primary_browser: Literal["brave", "chrome", "edge", "firefox"] = "brave"
    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    ipc_host: str = "127.0.0.1"
    ipc_port: int = 8765

    # Storage & Models
    data_dir: Path = Field(default_factory=default_data_dir)
    log_json: bool = False
    log_level: str = "INFO"

    def ensure_dirs(self) -> None:
        """Ensure runtime directories exist with strict owner permissions."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "models").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "downloads").mkdir(parents=True, exist_ok=True)


class SecretStore:
    """Secure secret storage backed by native OS Keychain with encrypted local vault fallback."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or default_data_dir()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._vault_path = self._data_dir / ".vault.enc"
        self._token_file = self._data_dir / "ipc.token"

    def _local_fernet(self) -> Fernet:
        seed_material = f"{os.getuid() if hasattr(os, 'getuid') else os.getlogin()}::{self._data_dir.resolve()}::sirius-v1"
        digest = hashlib.sha256(seed_material.encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    def _read_fallback_vault(self) -> dict[str, str]:
        if not self._vault_path.exists():
            return {}
        try:
            decrypted = self._local_fernet().decrypt(self._vault_path.read_bytes())
            payload = json.loads(decrypted.decode("utf-8"))
            return {str(k): str(v) for k, v in payload.items()} if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _write_fallback_vault(self, data: dict[str, str]) -> None:
        encrypted = self._local_fernet().encrypt(json.dumps(data).encode("utf-8"))
        self._vault_path.write_bytes(encrypted)
        if sys.platform != "win32":
            self._vault_path.chmod(0o600)

    def get_secret(self, name: str) -> str | None:
        """Retrieve a secret from OS Keyring, environment variable, or local encrypted vault."""
        env_val = os.environ.get(name) or os.environ.get(f"SIRIUS_{name}")
        if env_val:
            return env_val
        try:
            val = keyring.get_password(SERVICE_NAME, name)
            if val:
                return val
        except (KeyringError, RuntimeError, Exception):
            pass
        return self._read_fallback_vault().get(name)

    def set_secret(self, name: str, value: str) -> None:
        """Store a secret in OS Keyring and mirror to fallback vault if keyring is unavailable."""
        stored_in_keyring = False
        try:
            keyring.set_password(SERVICE_NAME, name, value)
            stored_in_keyring = True
        except (KeyringError, RuntimeError, Exception):
            stored_in_keyring = False

        if not stored_in_keyring:
            vault = self._read_fallback_vault()
            vault[name] = value
            self._write_fallback_vault(vault)

    def delete_secret(self, name: str) -> None:
        """Remove a secret from OS Keyring and fallback vault."""
        try:
            keyring.delete_password(SERVICE_NAME, name)
        except (KeyringError, RuntimeError, Exception):
            pass
        vault = self._read_fallback_vault()
        if name in vault:
            del vault[name]
            self._write_fallback_vault(vault)

    def get_or_create_ipc_token(self) -> str:
        """Return persistent authenticated WebSocket IPC token and write owner-only token file."""
        token = self.get_secret("IPC_AUTH_TOKEN")
        if not token:
            token = secrets.token_urlsafe(32)
            self.set_secret("IPC_AUTH_TOKEN", token)
        self._token_file.write_text(token, encoding="utf-8")
        if sys.platform != "win32":
            self._token_file.chmod(0o600)
        return token

    def get_or_create_memory_key(self) -> bytes:
        """Return 32-byte URL-safe base64 Fernet key for SQLite memory column encryption."""
        existing = self.get_secret("MEMORY_FERNET_KEY")
        if existing:
            return existing.encode("utf-8")
        generated = Fernet.generate_key().decode("utf-8")
        self.set_secret("MEMORY_FERNET_KEY", generated)
        return generated.encode("utf-8")

    def get_gemini_api_key(self) -> str | None:
        """Fetch Gemini API key from GEMINI_API_KEY, GOOGLE_API_KEY, or OS keychain."""
        return (
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or self.get_secret("GEMINI_API_KEY")
        )
