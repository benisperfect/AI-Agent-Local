import React from "react";

interface OnboardingWizardProps {
  osName: string;
  permissions: Record<string, boolean>;
}

const PERMISSION_ITEMS: Array<{
  key: string;
  label: string;
  hint: string;
}> = [
  {
    key: "microphone",
    label: "AUDIO_MIC_FULL_DUPLEX",
    hint: "Local wake-word ('Hey Sirius'), Silero VAD, and Gemini 3.8 Live voice.",
  },
  {
    key: "accessibility",
    label: "TIER2_ACCESSIBILITY_TREE",
    hint: "AT-SPI2 / AXUIElement / UIA zero-cursor semantic UI invocation.",
  },
  {
    key: "screen_recording",
    label: "TIER3_SCREEN_CAPTURE",
    hint: "Multi-monitor Set-of-Marks Vision grounding & visual verification.",
  },
  {
    key: "input_synthesis",
    label: "INPUT_SYNTHESIS_DRIVER",
    hint: "Wayland /dev/uinput, X11, macOS CGEvent, or Windows SendInput.",
  },
];

export const OnboardingWizard: React.FC<OnboardingWizardProps> = ({
  osName,
  permissions,
}) => {
  return (
    <div className="space-y-2.5 text-[11px]">
      <div className="flex items-center justify-between text-emerald-400 border-b border-emerald-950 pb-1.5">
        <span>$ sirius --doctor</span>
        <span className="px-1.5 py-0.5 rounded bg-black/70 border border-emerald-900 text-cyan-300">
          {osName || "detecting..."}
        </span>
      </div>

      <div className="space-y-1.5">
        {PERMISSION_ITEMS.map((item) => {
          const ok = Boolean(permissions[item.key]);
          return (
            <div
              key={item.key}
              className="p-2 rounded bg-black/60 border border-emerald-950 flex items-start gap-2"
            >
              <span
                className={`font-bold shrink-0 ${
                  ok ? "text-emerald-400" : "text-amber-400"
                }`}
              >
                {ok ? "[ OK ]" : "[WARN]"}
              </span>
              <div>
                <div className="font-bold text-emerald-100">{item.label}</div>
                <div className="text-[10px] text-emerald-500/80 mt-0.5">
                  {item.hint}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="p-2 rounded bg-black/50 border border-emerald-950/90 text-[10px] text-emerald-500/90 space-y-1">
        <div className="text-emerald-300 font-bold"># OS PERMISSION NOTES</div>
        <div>- macOS: System Settings -&gt; Privacy &amp; Security -&gt; Accessibility + Screen Recording</div>
        <div>- Linux Wayland: add user to <span className="text-cyan-300">input</span> group for <span className="text-cyan-300">/dev/uinput</span></div>
        <div>- Windows: UIA works natively (elevate only for Admin apps)</div>
      </div>
    </div>
  );
};
