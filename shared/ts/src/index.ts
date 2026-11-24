import { z } from "zod";

export const AgentStateSchema = z.enum([
  "SLEEPING",
  "LISTENING",
  "THINKING",
  "EXECUTING",
  "SPEAKING",
  "AWAITING_CONFIRMATION",
]);
export type AgentState = z.infer<typeof AgentStateSchema>;

export const RiskLevelSchema = z.enum([
  "LOW",
  "MEDIUM",
  "HIGH_IRREVERSIBLE",
  "BLOCKED",
]);
export type RiskLevel = z.infer<typeof RiskLevelSchema>;

export const ControlTierSchema = z.enum([
  "TIER1_NATIVE",
  "TIER2_ACCESSIBILITY",
  "TIER3_VISION",
  "BROWSER_CDP",
  "BROWSER_EXTENSION",
  "MCP_TOOL",
  "MEMORY",
]);
export type ControlTier = z.infer<typeof ControlTierSchema>;

export const ClientRoleSchema = z.enum(["hud", "browser_extension", "cli"]);
export type ClientRole = z.infer<typeof ClientRoleSchema>;

export const PlanStepSchema = z.object({
  step_id: z.string(),
  description: z.string(),
  tier: ControlTierSchema,
  status: z.enum(["pending", "running", "verified", "failed", "skipped"]),
});
export type PlanStep = z.infer<typeof PlanStepSchema>;

export const MemoryEntrySchema = z.object({
  memory_id: z.string(),
  category: z.string(),
  key: z.string(),
  value: z.string(),
  updated_at: z.string(),
});
export type MemoryEntry = z.infer<typeof MemoryEntrySchema>;

export const VoiceMacroSchema = z.object({
  macro_id: z.string(),
  trigger_phrase: z.string(),
  description: z.string(),
  steps: z.array(z.string()),
  updated_at: z.string(),
});
export type VoiceMacro = z.infer<typeof VoiceMacroSchema>;

// Daemon -> Client messages
export const ServerMessageSchema = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("auth_ok"),
    client_role: ClientRoleSchema,
    daemon_version: z.string(),
    session_id: z.string(),
  }),
  z.object({
    type: z.literal("state_changed"),
    state: AgentStateSchema,
    previous_state: AgentStateSchema,
    reason: z.string(),
    timestamp: z.string(),
  }),
  z.object({
    type: z.literal("audio_level"),
    mic_rms: z.number(),
    speaker_rms: z.number(),
    vad_probability: z.number(),
    aec_attenuation_db: z.number(),
  }),
  z.object({
    type: z.literal("transcript"),
    role: z.enum(["user", "assistant", "system"]),
    text: z.string(),
    final: z.boolean(),
    timestamp: z.string(),
  }),
  z.object({
    type: z.literal("tool_activity"),
    call_id: z.string(),
    tool_name: z.string(),
    tier: ControlTierSchema,
    risk_level: RiskLevelSchema,
    status: z.enum([
      "running",
      "succeeded",
      "failed",
      "cancelled",
      "awaiting_confirmation",
      "blocked",
    ]),
    summary: z.string(),
    verified: z.boolean().nullable(),
    timestamp: z.string(),
  }),
  z.object({
    type: z.literal("confirmation_request"),
    request_id: z.string(),
    call_id: z.string(),
    tool_name: z.string(),
    risk_level: RiskLevelSchema,
    spoken_prompt: z.string(),
    action_details: z.record(z.unknown()),
    expires_in_seconds: z.number(),
    timestamp: z.string(),
  }),
  z.object({
    type: z.literal("plan_update"),
    plan_id: z.string(),
    goal: z.string(),
    steps: z.array(PlanStepSchema),
  }),
  z.object({
    type: z.literal("system_status"),
    os_name: z.string(),
    wake_word: z.string(),
    cloud_provider: z.string(),
    offline_mode: z.boolean(),
    browser_connected: z.boolean(),
    browser_mode: z.enum(["cdp", "extension", "disconnected"]),
    active_browser: z.string(),
    permissions: z.record(z.boolean()),
    memory_count: z.number(),
    macro_count: z.number(),
  }),
  z.object({
    type: z.literal("memory_snapshot"),
    memories: z.array(MemoryEntrySchema),
    macros: z.array(VoiceMacroSchema),
  }),
  z.object({
    type: z.literal("extension_command"),
    request_id: z.string(),
    action: z.string(),
    payload: z.record(z.unknown()),
  }),
  z.object({
    type: z.literal("error"),
    code: z.string(),
    message: z.string(),
    recoverable: z.boolean(),
  }),
]);
export type ServerMessage = z.infer<typeof ServerMessageSchema>;

// Client -> Daemon messages
export const ClientMessageSchema = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("auth"),
    token: z.string(),
    client_role: ClientRoleSchema,
    metadata: z.record(z.unknown()).optional(),
  }),
  z.object({ type: z.literal("wake") }),
  z.object({ type: z.literal("sleep") }),
  z.object({
    type: z.literal("emergency_stop"),
    source: z.string(),
  }),
  z.object({
    type: z.literal("text_command"),
    text: z.string(),
  }),
  z.object({
    type: z.literal("confirmation_response"),
    request_id: z.string(),
    approved: z.boolean(),
    source: z.enum(["voice", "hud"]),
  }),
  z.object({
    type: z.literal("memory_upsert"),
    category: z.string(),
    key: z.string(),
    value: z.string(),
  }),
  z.object({
    type: z.literal("memory_delete"),
    memory_id: z.string(),
  }),
  z.object({
    type: z.literal("macro_upsert"),
    trigger_phrase: z.string(),
    description: z.string(),
    steps: z.array(z.string()),
  }),
  z.object({
    type: z.literal("macro_delete"),
    macro_id: z.string(),
  }),
  z.object({
    type: z.literal("update_settings"),
    wake_word: z.string().optional(),
    voice_name: z.string().optional(),
    primary_browser: z.string().optional(),
    offline_mode: z.boolean().optional(),
    api_key: z.string().optional(),
  }),
  z.object({ type: z.literal("request_snapshot") }),
  z.object({
    type: z.literal("extension_result"),
    request_id: z.string(),
    ok: z.boolean(),
    data: z.record(z.unknown()).optional(),
    error: z.string().optional(),
  }),
]);
export type ClientMessage = z.infer<typeof ClientMessageSchema>;
