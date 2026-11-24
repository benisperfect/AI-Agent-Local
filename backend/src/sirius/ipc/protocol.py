"""Strict Pydantic v2 IPC wire schemas shared with Tauri v2 HUD and Browser Extension."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AgentState(StrEnum):
    SLEEPING = "SLEEPING"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    EXECUTING = "EXECUTING"
    SPEAKING = "SPEAKING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH_IRREVERSIBLE = "HIGH_IRREVERSIBLE"
    BLOCKED = "BLOCKED"


class ControlTier(StrEnum):
    TIER1_NATIVE = "TIER1_NATIVE"
    TIER2_ACCESSIBILITY = "TIER2_ACCESSIBILITY"
    TIER3_VISION = "TIER3_VISION"
    BROWSER_CDP = "BROWSER_CDP"
    BROWSER_EXTENSION = "BROWSER_EXTENSION"
    MCP_TOOL = "MCP_TOOL"
    MEMORY = "MEMORY"


ClientRole = Literal["hud", "browser_extension", "cli"]


class PlanStepModel(BaseModel):
    step_id: str
    description: str
    tier: ControlTier
    status: Literal["pending", "running", "verified", "failed", "skipped"] = "pending"


class MemoryEntryModel(BaseModel):
    memory_id: str
    category: str
    key: str
    value: str
    updated_at: str


class VoiceMacroModel(BaseModel):
    macro_id: str
    trigger_phrase: str
    description: str
    steps: list[str]
    updated_at: str


# ---------------------------------------------------------------------------
# Daemon -> Client Server Messages
# ---------------------------------------------------------------------------


class AuthOkMessage(BaseModel):
    type: Literal["auth_ok"] = "auth_ok"
    client_role: ClientRole
    daemon_version: str
    session_id: str


class StateChangedMessage(BaseModel):
    type: Literal["state_changed"] = "state_changed"
    state: AgentState
    previous_state: AgentState
    reason: str
    timestamp: str = Field(default_factory=utc_now_iso)


class AudioLevelMessage(BaseModel):
    type: Literal["audio_level"] = "audio_level"
    mic_rms: float
    speaker_rms: float
    vad_probability: float
    aec_attenuation_db: float = 0.0


class TranscriptMessage(BaseModel):
    type: Literal["transcript"] = "transcript"
    role: Literal["user", "assistant", "system"]
    text: str
    final: bool = True
    timestamp: str = Field(default_factory=utc_now_iso)


class ToolActivityMessage(BaseModel):
    type: Literal["tool_activity"] = "tool_activity"
    call_id: str
    tool_name: str
    tier: ControlTier
    risk_level: RiskLevel
    status: Literal[
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "awaiting_confirmation",
        "blocked",
    ]
    summary: str
    verified: bool | None = None
    timestamp: str = Field(default_factory=utc_now_iso)


class ConfirmationRequestMessage(BaseModel):
    type: Literal["confirmation_request"] = "confirmation_request"
    request_id: str
    call_id: str
    tool_name: str
    risk_level: RiskLevel
    spoken_prompt: str
    action_details: dict[str, Any] = Field(default_factory=dict)
    expires_in_seconds: float = 25.0
    timestamp: str = Field(default_factory=utc_now_iso)


class PlanUpdateMessage(BaseModel):
    type: Literal["plan_update"] = "plan_update"
    plan_id: str
    goal: str
    steps: list[PlanStepModel]


class SystemStatusMessage(BaseModel):
    type: Literal["system_status"] = "system_status"
    os_name: str
    wake_word: str
    cloud_provider: str
    offline_mode: bool
    browser_connected: bool
    browser_mode: Literal["cdp", "extension", "disconnected"]
    active_browser: str
    permissions: dict[str, bool] = Field(default_factory=dict)
    memory_count: int = 0
    macro_count: int = 0


class MemorySnapshotMessage(BaseModel):
    type: Literal["memory_snapshot"] = "memory_snapshot"
    memories: list[MemoryEntryModel]
    macros: list[VoiceMacroModel]


class ExtensionCommandMessage(BaseModel):
    type: Literal["extension_command"] = "extension_command"
    request_id: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    recoverable: bool = True


ServerMessage = Annotated[
    AuthOkMessage
    | StateChangedMessage
    | AudioLevelMessage
    | TranscriptMessage
    | ToolActivityMessage
    | ConfirmationRequestMessage
    | PlanUpdateMessage
    | SystemStatusMessage
    | MemorySnapshotMessage
    | ExtensionCommandMessage
    | ErrorMessage,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Client -> Daemon Messages
# ---------------------------------------------------------------------------


class AuthClientMessage(BaseModel):
    type: Literal["auth"] = "auth"
    token: str
    client_role: ClientRole
    metadata: dict[str, Any] = Field(default_factory=dict)


class WakeClientMessage(BaseModel):
    type: Literal["wake"] = "wake"


class SleepClientMessage(BaseModel):
    type: Literal["sleep"] = "sleep"


class EmergencyStopMessage(BaseModel):
    type: Literal["emergency_stop"] = "emergency_stop"
    source: str = "hud"


class TextCommandMessage(BaseModel):
    type: Literal["text_command"] = "text_command"
    text: str


class ConfirmationResponseMessage(BaseModel):
    type: Literal["confirmation_response"] = "confirmation_response"
    request_id: str
    approved: bool
    source: Literal["voice", "hud"] = "hud"


class MemoryUpsertMessage(BaseModel):
    type: Literal["memory_upsert"] = "memory_upsert"
    category: str
    key: str
    value: str


class MemoryDeleteMessage(BaseModel):
    type: Literal["memory_delete"] = "memory_delete"
    memory_id: str


class MacroUpsertMessage(BaseModel):
    type: Literal["macro_upsert"] = "macro_upsert"
    trigger_phrase: str
    description: str
    steps: list[str]


class MacroDeleteMessage(BaseModel):
    type: Literal["macro_delete"] = "macro_delete"
    macro_id: str


class UpdateSettingsMessage(BaseModel):
    type: Literal["update_settings"] = "update_settings"
    wake_word: str | None = None
    voice_name: str | None = None
    primary_browser: Literal["brave", "chrome", "edge", "firefox"] | None = None
    offline_mode: bool | None = None
    api_key: str | None = None


class RequestSnapshotMessage(BaseModel):
    type: Literal["request_snapshot"] = "request_snapshot"


class ExtensionResultMessage(BaseModel):
    type: Literal["extension_result"] = "extension_result"
    request_id: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


ClientMessage = Annotated[
    AuthClientMessage
    | WakeClientMessage
    | SleepClientMessage
    | EmergencyStopMessage
    | TextCommandMessage
    | ConfirmationResponseMessage
    | MemoryUpsertMessage
    | MemoryDeleteMessage
    | MacroUpsertMessage
    | MacroDeleteMessage
    | UpdateSettingsMessage
    | RequestSnapshotMessage
    | ExtensionResultMessage,
    Field(discriminator="type"),
]

_CLIENT_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(raw_json: str | bytes) -> ClientMessage:
    """Validate and deserialize raw JSON into a discriminated ClientMessage."""
    return _CLIENT_ADAPTER.validate_json(raw_json)
