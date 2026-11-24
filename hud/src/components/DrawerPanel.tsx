import React, { useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import type { MemoryEntry, PlanStep, VoiceMacro } from "../types/ipc";
import type { SystemStatusSnapshot, ToolLogItem } from "../hooks/useSiriusSocket";
import { OnboardingWizard } from "./OnboardingWizard";
import { isTauriRuntime } from "../lib/tauriBridge";

type TabId = "activity" | "memory" | "browser" | "permissions" | "settings";

interface DrawerPanelProps {
  connected: boolean;
  planGoal: string;
  planSteps: PlanStep[];
  toolHistory: ToolLogItem[];
  systemStatus: SystemStatusSnapshot | null;
  memories: MemoryEntry[];
  macros: VoiceMacro[];
  onSendTextCommand: (text: string) => void;
  onUpsertMemory: (category: string, key: string, value: string) => void;
  onDeleteMemory: (id: string) => void;
  onUpsertMacro: (trigger: string, description: string, steps: string[]) => void;
  onDeleteMacro: (id: string) => void;
  onUpdateSettings: (payload: {
    wake_word?: string;
    voice_name?: string;
    primary_browser?: string;
    offline_mode?: boolean;
    api_key?: string;
  }) => void;
  onSaveDevToken: (token: string) => void;
}

export const DrawerPanel: React.FC<DrawerPanelProps> = ({
  connected,
  planGoal,
  planSteps,
  toolHistory,
  systemStatus,
  memories,
  macros,
  onSendTextCommand,
  onUpsertMemory,
  onDeleteMemory,
  onUpsertMacro,
  onDeleteMacro,
  onUpdateSettings,
  onSaveDevToken,
}) => {
  const [activeTab, setActiveTab] = useState<TabId>("activity");
  const [commandInput, setCommandInput] = useState("");

  // Memory form state
  const [memCategory, setMemCategory] = useState("contacts");
  const [memKey, setMemKey] = useState("");
  const [memValue, setMemValue] = useState("");

  // Macro form state
  const [macroTrigger, setMacroTrigger] = useState("");
  const [macroStepsText, setMacroStepsText] = useState("");

  // Settings form state (synced with daemon systemStatus)
  const [wakeWordInput, setWakeWordInput] = useState("hey sirius");
  const [voiceSelect, setVoiceSelect] = useState("Aoede");
  const [browserSelect, setBrowserSelect] = useState("brave");
  const [offlineMode, setOfflineMode] = useState(false);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [devTokenInput, setDevTokenInput] = useState("");
  const [savedNotice, setSavedNotice] = useState("");

  useEffect(() => {
    if (!systemStatus) return;
    setWakeWordInput(systemStatus.wakeWord);
    setBrowserSelect(systemStatus.activeBrowser);
    setOfflineMode(systemStatus.offlineMode);
  }, [
    systemStatus?.wakeWord,
    systemStatus?.activeBrowser,
    systemStatus?.offlineMode,
  ]);

  const tabs: Array<{ id: TabId; label: string }> = [
    { id: "activity", label: "tty" },
    { id: "memory", label: "mem" },
    { id: "browser", label: "web" },
    { id: "permissions", label: "sys" },
    { id: "settings", label: "cfg" },
  ];

  return (
    <div className="terminal-surface mt-1.5 rounded-md border border-emerald-900/90 shadow-2xl overflow-hidden flex flex-col max-h-[485px] text-[11px] text-emerald-100">
      {/* Terminal Header Bar + Tabs */}
      <div className="flex items-center justify-between border-b border-emerald-950 px-2.5 py-1.5 bg-black/70">
        <div className="flex items-center gap-1">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className={`px-2 py-0.5 rounded font-bold transition ${
                activeTab === tab.id
                  ? "bg-emerald-500 text-black"
                  : "text-emerald-500 hover:text-emerald-300 hover:bg-emerald-950/60"
              }`}
            >
              /{tab.label}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-emerald-600">
          {systemStatus?.offlineMode ? "MODE:LOCAL" : "MODE:GEMINI-3.8"}
        </span>
      </div>

      {/* Tab Body */}
      <div className="p-3 overflow-y-auto flex-1 space-y-3">
        {activeTab === "activity" && (
          <div className="space-y-3">
            {/* Terminal Command Prompt */}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (!commandInput.trim()) return;
                onSendTextCommand(commandInput.trim());
                setCommandInput("");
              }}
              className="flex items-center gap-1.5 px-2 py-1.5 rounded bg-black/80 border border-emerald-900 focus-within:border-emerald-400"
            >
              <span className="text-emerald-400 font-bold shrink-0">
                sirius@local:~$
              </span>
              <input
                type="text"
                value={commandInput}
                onChange={(e) => setCommandInput(e.target.value)}
                placeholder="enter command or macro..."
                className="flex-1 bg-transparent text-emerald-100 placeholder-emerald-800 focus:outline-none"
              />
              <button
                type="submit"
                disabled={!connected}
                className="px-2 py-0.5 rounded bg-emerald-500/90 hover:bg-emerald-400 disabled:opacity-40 text-black font-bold text-[10px]"
              >
                RUN
              </button>
            </form>

            {/* Active Plan Graph */}
            {planSteps.length > 0 && (
              <div className="p-2 rounded bg-black/70 border border-emerald-900/80 space-y-1">
                <div className="text-cyan-300 font-bold truncate">
                  # PLAN: {planGoal}
                </div>
                {planSteps.map((step) => (
                  <div
                    key={step.step_id}
                    className="flex items-center justify-between gap-2 text-[10px]"
                  >
                    <span className="truncate text-emerald-200">
                      ├─ {step.description}
                    </span>
                    <span className="text-amber-300 shrink-0">
                      [{step.status.toUpperCase()}]
                    </span>
                  </div>
                ))}
              </div>
            )}

            {/* Audit Log Stream */}
            <div className="space-y-1">
              <div className="text-[10px] font-bold text-emerald-600">
                # AUDIT_LOG ({toolHistory.length} events)
              </div>
              {toolHistory.length === 0 ? (
                <div className="text-emerald-700 py-4 text-center">
                  -- awaiting voice or terminal input --
                </div>
              ) : (
                toolHistory.map((item) => (
                  <div
                    key={`${item.callId}-${item.status}`}
                    className="p-1.5 rounded bg-black/60 border border-emerald-950 space-y-0.5"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-bold text-emerald-300">
                        $ {item.toolName}
                      </span>
                      <span className="text-[10px] text-cyan-400">
                        [{item.tier}:{item.status.toUpperCase()}]
                      </span>
                    </div>
                    <div className="text-[10px] text-emerald-500 truncate">
                      &gt; {item.summary}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {activeTab === "memory" && (
          <div className="space-y-3">
            <div className="p-2.5 rounded bg-black/70 border border-emerald-950 space-y-2">
              <div className="font-bold text-emerald-300">
                # ADD_ENCRYPTED_MEMORY (SQLite + Fernet)
              </div>
              <div className="grid grid-cols-3 gap-1.5">
                <input
                  value={memCategory}
                  onChange={(e) => setMemCategory(e.target.value)}
                  placeholder="category"
                  className="px-2 py-1 rounded bg-black border border-emerald-900 text-emerald-100"
                />
                <input
                  value={memKey}
                  onChange={(e) => setMemKey(e.target.value)}
                  placeholder="alias"
                  className="px-2 py-1 rounded bg-black border border-emerald-900 text-emerald-100"
                />
                <input
                  value={memValue}
                  onChange={(e) => setMemValue(e.target.value)}
                  placeholder="value"
                  className="px-2 py-1 rounded bg-black border border-emerald-900 text-emerald-100"
                />
              </div>
              <button
                type="button"
                onClick={() => {
                  if (!memKey.trim() || !memValue.trim()) return;
                  onUpsertMemory(memCategory, memKey, memValue);
                  setMemKey("");
                  setMemValue("");
                }}
                className="px-2.5 py-1 rounded bg-emerald-500 hover:bg-emerald-400 text-black font-bold text-[10px]"
              >
                + WRITE_MEMORY
              </button>
            </div>

            <div className="space-y-1">
              {memories.map((m) => (
                <div
                  key={m.memory_id}
                  className="flex items-center justify-between p-1.5 rounded bg-black/60 border border-emerald-950"
                >
                  <div className="truncate pr-2">
                    <span className="text-cyan-400">[{m.category}]</span>{" "}
                    <span className="text-emerald-300 font-bold">{m.key}=</span>
                    {m.value}
                  </div>
                  <button
                    type="button"
                    onClick={() => onDeleteMemory(m.memory_id)}
                    className="text-emerald-700 hover:text-rose-400"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>

            <div className="p-2.5 rounded bg-black/70 border border-emerald-950 space-y-2">
              <div className="font-bold text-emerald-300">
                # VOICE_MACROS ({macros.length})
              </div>
              <input
                value={macroTrigger}
                onChange={(e) => setMacroTrigger(e.target.value)}
                placeholder="trigger phrase (e.g. start focus mode)"
                className="w-full px-2 py-1 rounded bg-black border border-emerald-900"
              />
              <input
                value={macroStepsText}
                onChange={(e) => setMacroStepsText(e.target.value)}
                placeholder="steps separated by commas (close Slack, set volume to 25)"
                className="w-full px-2 py-1 rounded bg-black border border-emerald-900"
              />
              <button
                type="button"
                onClick={() => {
                  const steps = macroStepsText
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean);
                  if (!macroTrigger.trim() || steps.length === 0) return;
                  onUpsertMacro(macroTrigger, `Voice macro: ${macroTrigger}`, steps);
                  setMacroTrigger("");
                  setMacroStepsText("");
                }}
                className="px-2.5 py-1 rounded bg-cyan-400 hover:bg-cyan-300 text-black font-bold text-[10px]"
              >
                + SAVE_MACRO
              </button>

              {macros.map((mac) => (
                <div
                  key={mac.macro_id}
                  className="flex items-center justify-between p-1.5 rounded bg-black/80 border border-emerald-950"
                >
                  <div className="truncate pr-2">
                    <span className="text-amber-300">&quot;{mac.trigger_phrase}&quot;</span> -&gt;{" "}
                    {mac.steps.join(" ; ")}
                  </div>
                  <button
                    type="button"
                    onClick={() => onDeleteMacro(mac.macro_id)}
                    className="text-emerald-700 hover:text-rose-400"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {activeTab === "browser" && (
          <div className="space-y-2.5">
            <div className="p-2.5 rounded bg-black/70 border border-emerald-950 space-y-1">
              <div className="flex items-center justify-between">
                <span className="text-emerald-500">PRIMARY_BROWSER</span>
                <span className="text-cyan-300 font-bold uppercase">
                  {systemStatus?.activeBrowser ?? "brave"}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-emerald-500">BRIDGE_STATUS</span>
                <span
                  className={`font-bold ${
                    systemStatus?.browserConnected
                      ? "text-emerald-400"
                      : "text-amber-400"
                  }`}
                >
                  [{(systemStatus?.browserMode ?? "disconnected").toUpperCase()}]
                </span>
              </div>
            </div>

            <div className="p-2.5 rounded bg-black/50 border border-emerald-950 text-[10px] text-emerald-400/90 space-y-1">
              <div className="font-bold text-emerald-300"># ATTACH LIVE BRAVE SESSION</div>
              <div>1. MV3 Extension: brave://extensions -&gt; Load unpacked -&gt; sirius/browser-extension</div>
              <div>2. Playwright CDP: brave-browser --remote-debugging-port=9222</div>
            </div>
          </div>
        )}

        {activeTab === "permissions" && (
          <OnboardingWizard
            osName={systemStatus?.osName ?? ""}
            permissions={systemStatus?.permissions ?? {}}
          />
        )}

        {activeTab === "settings" && (
          <div className="space-y-2.5">
            <div className="space-y-1">
              <label className="text-emerald-500 block">WAKE_WORD</label>
              <input
                value={wakeWordInput}
                onChange={(e) => setWakeWordInput(e.target.value)}
                className="w-full px-2 py-1.5 rounded bg-black border border-emerald-900 text-emerald-100 focus:border-emerald-400 focus:outline-none"
              />
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-emerald-500 block mb-1">GEMINI_VOICE</label>
                <select
                  value={voiceSelect}
                  onChange={(e) => setVoiceSelect(e.target.value)}
                  className="w-full px-2 py-1.5 rounded bg-black border border-emerald-900 text-emerald-100"
                >
                  <option value="Aoede">Aoede (Calm)</option>
                  <option value="Kore">Kore (Composed)</option>
                  <option value="Charon">Charon (Deep)</option>
                  <option value="Puck">Puck (Crisp)</option>
                </select>
              </div>

              <div>
                <label className="text-emerald-500 block mb-1">PRIMARY_BROWSER</label>
                <select
                  value={browserSelect}
                  onChange={(e) => setBrowserSelect(e.target.value)}
                  className="w-full px-2 py-1.5 rounded bg-black border border-emerald-900 text-emerald-100"
                >
                  <option value="brave">brave</option>
                  <option value="chrome">chrome</option>
                  <option value="edge">edge</option>
                  <option value="firefox">firefox</option>
                </select>
              </div>
            </div>

            <label className="flex items-center justify-between p-2 rounded bg-black/60 border border-emerald-950 cursor-pointer">
              <span className="text-emerald-300">OFFLINE_LOCAL_MODE</span>
              <input
                type="checkbox"
                checked={offlineMode}
                onChange={(e) => setOfflineMode(e.target.checked)}
                className="accent-emerald-400"
              />
            </label>

            <div className="space-y-1">
              <label className="text-emerald-500 block">
                GEMINI_API_KEY (OS Keychain)
              </label>
              <input
                type="password"
                value={apiKeyInput}
                onChange={(e) => setApiKeyInput(e.target.value)}
                placeholder="AIza... (optional — auto-enables Gemini 3.8 Live)"
                className="w-full px-2 py-1.5 rounded bg-black border border-emerald-900 text-emerald-100 focus:border-emerald-400 focus:outline-none"
              />
            </div>

            <button
              type="button"
              onClick={() => {
                const trimmedKey = apiKeyInput.trim();
                const nextOffline = trimmedKey ? false : offlineMode;
                setOfflineMode(nextOffline);
                onUpdateSettings({
                  wake_word: wakeWordInput.trim(),
                  voice_name: voiceSelect,
                  primary_browser: browserSelect,
                  offline_mode: nextOffline,
                  api_key: trimmedKey || undefined,
                });
                setApiKeyInput("");
                setSavedNotice("[OK] Configuration applied to daemon.");
                window.setTimeout(() => setSavedNotice(""), 3000);
              }}
              className="w-full py-1.5 rounded bg-emerald-500 hover:bg-emerald-400 text-black font-bold transition"
            >
              $ APPLY_CONFIG
            </button>

            {savedNotice && (
              <div className="text-emerald-400 font-bold text-center">
                {savedNotice}
              </div>
            )}

            {!isTauriRuntime() && (
              <div className="pt-2 border-t border-emerald-950 space-y-1">
                <label className="text-emerald-600 block">
                  IPC_TOKEN (~/.local/share/sirius/ipc.token)
                </label>
                <div className="flex gap-1.5">
                  <input
                    type="password"
                    value={devTokenInput}
                    onChange={(e) => setDevTokenInput(e.target.value)}
                    placeholder="paste token..."
                    className="flex-1 px-2 py-1 rounded bg-black border border-emerald-900"
                  />
                  <button
                    type="button"
                    onClick={() => onSaveDevToken(devTokenInput)}
                    className="px-2 py-1 rounded bg-emerald-900 hover:bg-emerald-800 text-emerald-100 font-bold"
                  >
                    AUTH
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
