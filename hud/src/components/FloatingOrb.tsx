import React, { useEffect, useRef } from "react";
import { ChevronDown, ChevronUp, OctagonX } from "lucide-react";
import type { AgentState } from "../types/ipc";
import type { AudioTelemetry } from "../hooks/useSiriusSocket";
import { startWindowDrag } from "../lib/tauriBridge";

interface FloatingOrbProps {
  connected: boolean;
  state: AgentState;
  audio: AudioTelemetry;
  subtitle: string;
  drawerOpen: boolean;
  onToggleWake: () => void;
  onEmergencyStop: () => void;
  onToggleDrawer: () => void;
}

const STATE_TOKENS: Record<AgentState, string> = {
  SLEEPING: "IDLE",
  LISTENING: "LISTEN",
  THINKING: "THINK",
  EXECUTING: "EXEC",
  SPEAKING: "SPEAK",
  AWAITING_CONFIRMATION: "CONFIRM?",
};

const STATE_BORDERS: Record<AgentState, string> = {
  SLEEPING: "border-emerald-900/80 text-emerald-400/80",
  LISTENING: "border-emerald-400 text-emerald-300 shadow-[0_0_16px_rgba(16,185,129,0.22)]",
  THINKING: "border-amber-400 text-amber-300 shadow-[0_0_16px_rgba(245,158,11,0.2)]",
  EXECUTING: "border-cyan-400 text-cyan-300 shadow-[0_0_16px_rgba(34,211,238,0.22)]",
  SPEAKING: "border-emerald-300 text-emerald-200 shadow-[0_0_16px_rgba(52,211,153,0.25)]",
  AWAITING_CONFIRMATION: "border-rose-500 text-rose-300 shadow-[0_0_18px_rgba(244,63,94,0.3)]",
};

export const FloatingOrb: React.FC<FloatingOrbProps> = ({
  connected,
  state,
  audio,
  subtitle,
  drawerOpen,
  onToggleWake,
  onEmergencyStop,
  onToggleDrawer,
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const phaseRef = useRef<number>(0);

  useEffect(() => {
    let rafId = 0;
    const renderOscilloscope = () => {
      const canvas = canvasRef.current;
      if (canvas) {
        const ctx = canvas.getContext("2d");
        if (ctx) {
          const w = canvas.width;
          const h = canvas.height;
          ctx.clearRect(0, 0, w, h);

          // Subtle center baseline
          ctx.strokeStyle = "rgba(16, 185, 129, 0.16)";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(0, h / 2);
          ctx.lineTo(w, h / 2);
          ctx.stroke();

          const energy = Math.min(
            1,
            Math.max(audio.micRms * 13, audio.speakerRms * 10, state === "SLEEPING" ? 0.05 : 0.18)
          );
          phaseRef.current += 0.2;

          ctx.lineWidth = 1.6;
          ctx.strokeStyle =
            state === "LISTENING" || state === "SPEAKING"
              ? "#34d399"
              : state === "EXECUTING"
                ? "#22d3ee"
                : state === "THINKING"
                  ? "#fbbf24"
                  : state === "AWAITING_CONFIRMATION"
                    ? "#fb7185"
                    : "#3f6254";

          ctx.beginPath();
          for (let x = 0; x < w; x++) {
            const norm = x / w;
            const env = Math.sin(norm * Math.PI);
            const y =
              h / 2 +
              Math.sin(norm * 16 + phaseRef.current) * env * energy * (h * 0.44);
            if (x === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
          }
          ctx.stroke();
        }
      }
      rafId = window.requestAnimationFrame(renderOscilloscope);
    };
    rafId = window.requestAnimationFrame(renderOscilloscope);
    return () => window.cancelAnimationFrame(rafId);
  }, [audio.micRms, audio.speakerRms, state]);

  return (
    <div
      onMouseDown={(e) => {
        if ((e.target as HTMLElement).closest("button")) return;
        void startWindowDrag();
      }}
      className={`terminal-surface flex items-center gap-2 px-2.5 py-2 rounded-md border shadow-2xl transition-all duration-150 cursor-grab active:cursor-grabbing ${STATE_BORDERS[state]}`}
    >
      {/* Terminal Prompt State Toggle */}
      <button
        type="button"
        onClick={onToggleWake}
        title={state === "SLEEPING" ? "Wake Sirius" : "Put Sirius to sleep"}
        className="flex items-center gap-1.5 px-2 py-1 rounded bg-emerald-950/60 hover:bg-emerald-900/60 border border-emerald-800/70 text-[11px] font-bold tracking-wider shrink-0 transition"
      >
        <span
          className={`w-1.5 h-1.5 rounded-xs ${
            !connected
              ? "bg-rose-500"
              : state === "SLEEPING"
                ? "bg-emerald-700"
                : "bg-emerald-400 animate-pulse"
          }`}
        />
        <span>[{connected ? STATE_TOKENS[state] : "OFFLINE"}]</span>
      </button>

      {/* Oscilloscope Waveform */}
      <canvas
        ref={canvasRef}
        width={58}
        height={22}
        className="shrink-0 bg-black/50 rounded border border-emerald-950/90"
      />

      {/* Console Live Line */}
      <div className="flex-1 min-w-0 text-[11px] text-emerald-100/95 truncate">
        <span className="text-emerald-500 mr-1">&gt;</span>
        {subtitle}
      </div>

      {/* Emergency Stop */}
      {state !== "SLEEPING" && (
        <button
          type="button"
          onClick={onEmergencyStop}
          title="Emergency Stop (Ctrl+Shift+Space)"
          className="px-1.5 py-1 rounded bg-rose-950/80 hover:bg-rose-900 border border-rose-700/80 text-rose-300 text-[10px] font-bold flex items-center gap-1 shrink-0 transition"
        >
          <OctagonX className="w-3 h-3" />
          <span>KILL</span>
        </button>
      )}

      {/* Expand/Collapse Console Drawer */}
      <button
        type="button"
        onClick={onToggleDrawer}
        title="Toggle Sirius Terminal Drawer"
        className="p-1 rounded bg-black/60 hover:bg-emerald-950 border border-emerald-900/80 text-emerald-400 shrink-0 transition"
      >
        {drawerOpen ? (
          <ChevronUp className="w-3.5 h-3.5" />
        ) : (
          <ChevronDown className="w-3.5 h-3.5" />
        )}
      </button>
    </div>
  );
};
