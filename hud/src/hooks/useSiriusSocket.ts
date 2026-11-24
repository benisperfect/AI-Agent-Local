import { useCallback, useEffect, useRef, useState } from "react";
import {
  type AgentState,
  type ClientMessage,
  type MemoryEntry,
  type PlanStep,
  type ServerMessage,
  ServerMessageSchema,
  type VoiceMacro,
} from "../types/ipc";
import { onNativeTrayEvent, readLocalIpcToken } from "../lib/tauriBridge";

export interface AudioTelemetry {
  micRms: number;
  speakerRms: number;
  vadProbability: number;
  aecAttenuationDb: number;
}

export interface ToolLogItem {
  callId: string;
  toolName: string;
  tier: string;
  riskLevel: string;
  status: string;
  summary: string;
  timestamp: string;
}

export interface PendingConfirmation {
  requestId: string;
  callId: string;
  toolName: string;
  riskLevel: string;
  spokenPrompt: string;
  actionDetails: Record<string, unknown>;
  expiresInSeconds: number;
}

export interface SystemStatusSnapshot {
  osName: string;
  wakeWord: string;
  cloudProvider: string;
  offlineMode: boolean;
  browserConnected: boolean;
  browserMode: "cdp" | "extension" | "disconnected";
  activeBrowser: string;
  permissions: Record<string, boolean>;
  memoryCount: number;
  macroCount: number;
}

export function useSiriusSocket(defaultPort = 8765) {
  const [connected, setConnected] = useState(false);
  const [agentState, setAgentState] = useState<AgentState>("SLEEPING");
  const [audio, setAudio] = useState<AudioTelemetry>({
    micRms: 0,
    speakerRms: 0,
    vadProbability: 0,
    aecAttenuationDb: 0,
  });
  const [subtitle, setSubtitle] = useState<string>('Say "Hey Sirius" to begin');
  const [toolHistory, setToolHistory] = useState<ToolLogItem[]>([]);
  const [planGoal, setPlanGoal] = useState<string>("");
  const [planSteps, setPlanSteps] = useState<PlanStep[]>([]);
  const [pendingConfirmation, setPendingConfirmation] =
    useState<PendingConfirmation | null>(null);
  const [systemStatus, setSystemStatus] = useState<SystemStatusSnapshot | null>(
    null
  );
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [macros, setMacros] = useState<VoiceMacro[]>([]);
  const [manualToken, setManualToken] = useState<string>("");

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);

  const sendMessage = useCallback((msg: ClientMessage) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }, []);

  const handleServerMessage = useCallback((msg: ServerMessage) => {
    switch (msg.type) {
      case "auth_ok":
        setConnected(true);
        wsRef.current?.send(JSON.stringify({ type: "request_snapshot" }));
        break;
      case "state_changed":
        setAgentState(msg.state);
        if (msg.state === "SLEEPING") {
          setSubtitle('Say "Hey Sirius" to wake');
        }
        if (msg.state !== "AWAITING_CONFIRMATION") {
          setPendingConfirmation(null);
        }
        break;
      case "audio_level":
        setAudio({
          micRms: msg.mic_rms,
          speakerRms: msg.speaker_rms,
          vadProbability: msg.vad_probability,
          aecAttenuationDb: msg.aec_attenuation_db,
        });
        break;
      case "transcript":
        setSubtitle(msg.text);
        break;
      case "tool_activity":
        setSubtitle(`[${msg.tier}] ${msg.summary}`);
        setToolHistory((prev) => [
          {
            callId: msg.call_id,
            toolName: msg.tool_name,
            tier: msg.tier,
            riskLevel: msg.risk_level,
            status: msg.status,
            summary: msg.summary,
            timestamp: msg.timestamp,
          },
          ...prev.slice(0, 79),
        ]);
        break;
      case "confirmation_request":
        setPendingConfirmation({
          requestId: msg.request_id,
          callId: msg.call_id,
          toolName: msg.tool_name,
          riskLevel: msg.risk_level,
          spokenPrompt: msg.spoken_prompt,
          actionDetails: msg.action_details,
          expiresInSeconds: msg.expires_in_seconds,
        });
        setSubtitle(msg.spoken_prompt);
        break;
      case "plan_update":
        setPlanGoal(msg.goal);
        setPlanSteps(msg.steps);
        break;
      case "system_status":
        setSystemStatus({
          osName: msg.os_name,
          wakeWord: msg.wake_word,
          cloudProvider: msg.cloud_provider,
          offlineMode: msg.offline_mode,
          browserConnected: msg.browser_connected,
          browserMode: msg.browser_mode,
          activeBrowser: msg.active_browser,
          permissions: msg.permissions,
          memoryCount: msg.memory_count,
          macroCount: msg.macro_count,
        });
        break;
      case "memory_snapshot":
        setMemories(msg.memories);
        setMacros(msg.macros);
        break;
      case "error":
        setSubtitle(`Error: ${msg.message}`);
        break;
      case "extension_command":
        break;
    }
  }, []);

  const connectSocket = useCallback(async () => {
    if (
      wsRef.current &&
      (wsRef.current.readyState === WebSocket.OPEN ||
        wsRef.current.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    const token = manualToken.trim() || (await readLocalIpcToken());
    if (!token) {
      reconnectTimerRef.current = window.setTimeout(() => {
        void connectSocket();
      }, 3000);
      return;
    }

    const ws = new WebSocket(`ws://127.0.0.1:${defaultPort}`);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          type: "auth",
          token,
          client_role: "hud",
        })
      );
    };

    ws.onmessage = (ev) => {
      try {
        const raw: unknown = JSON.parse(String(ev.data));
        const parsed = ServerMessageSchema.safeParse(raw);
        if (parsed.success) {
          handleServerMessage(parsed.data);
        }
      } catch {
        // Ignore malformed frame
      }
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      reconnectTimerRef.current = window.setTimeout(() => {
        void connectSocket();
      }, 2500);
    };
  }, [defaultPort, handleServerMessage, manualToken]);

  useEffect(() => {
    void connectSocket();
    return () => {
      if (reconnectTimerRef.current) {
        window.clearTimeout(reconnectTimerRef.current);
      }
      wsRef.current?.close();
    };
  }, [connectSocket]);

  // Wire native Tauri system tray & global emergency hotkey events
  useEffect(() => {
    let cleanupStop: () => void = () => {};
    let cleanupWake: () => void = () => {};

    void onNativeTrayEvent("sirius://emergency-stop", () => {
      sendMessage({ type: "emergency_stop", source: "tauri_global_shortcut" });
    }).then((fn) => {
      cleanupStop = fn;
    });

    void onNativeTrayEvent("sirius://wake", () => {
      sendMessage({ type: "wake" });
    }).then((fn) => {
      cleanupWake = fn;
    });

    return () => {
      cleanupStop();
      cleanupWake();
    };
  }, [sendMessage]);

  const saveDevToken = useCallback((token: string) => {
    localStorage.setItem("sirius_dev_ipc_token", token.trim());
    setManualToken(token.trim());
    wsRef.current?.close();
  }, []);

  return {
    connected,
    agentState,
    audio,
    subtitle,
    toolHistory,
    planGoal,
    planSteps,
    pendingConfirmation,
    systemStatus,
    memories,
    macros,
    saveDevToken,
    wake: () => sendMessage({ type: "wake" }),
    sleep: () => sendMessage({ type: "sleep" }),
    emergencyStop: () =>
      sendMessage({ type: "emergency_stop", source: "hud_button" }),
    sendTextCommand: (text: string) =>
      sendMessage({ type: "text_command", text }),
    respondConfirmation: (requestId: string, approved: boolean) =>
      sendMessage({
        type: "confirmation_response",
        request_id: requestId,
        approved,
        source: "hud",
      }),
    upsertMemory: (category: string, key: string, value: string) =>
      sendMessage({ type: "memory_upsert", category, key, value }),
    deleteMemory: (memoryId: string) =>
      sendMessage({ type: "memory_delete", memory_id: memoryId }),
    upsertMacro: (triggerPhrase: string, description: string, steps: string[]) =>
      sendMessage({
        type: "macro_upsert",
        trigger_phrase: triggerPhrase,
        description,
        steps,
      }),
    deleteMacro: (macroId: string) =>
      sendMessage({ type: "macro_delete", macro_id: macroId }),
    updateSettings: (payload: {
      wake_word?: string;
      voice_name?: string;
      primary_browser?: string;
      offline_mode?: boolean;
      api_key?: string;
    }) => sendMessage({ type: "update_settings", ...payload }),
  };
}
