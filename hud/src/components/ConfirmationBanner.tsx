import React from "react";
import type { PendingConfirmation } from "../hooks/useSiriusSocket";

interface ConfirmationBannerProps {
  confirmation: PendingConfirmation;
  onRespond: (requestId: string, approved: boolean) => void;
}

export const ConfirmationBanner: React.FC<ConfirmationBannerProps> = ({
  confirmation,
  onRespond,
}) => {
  return (
    <div className="terminal-surface mt-2 rounded-md border border-rose-500/80 p-3 shadow-2xl text-[11px]">
      <div className="flex items-center justify-between gap-2 text-rose-400 font-bold">
        <span>[!] HIGH_IRREVERSIBLE :: CONFIRMATION REQUIRED</span>
        <span className="px-1.5 py-0.5 rounded bg-rose-950/90 border border-rose-800 text-[10px]">
          {confirmation.toolName}
        </span>
      </div>
      <p className="mt-1.5 text-emerald-100 leading-snug">
        <span className="text-rose-400 mr-1">&gt;</span>
        {confirmation.spokenPrompt}
      </p>
      <div className="mt-2.5 flex items-center justify-between gap-2">
        <span className="text-[10px] text-emerald-500/80">
          Say &ldquo;Yes, go ahead&rdquo; or &ldquo;No, cancel&rdquo;
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onRespond(confirmation.requestId, false)}
            className="px-2.5 py-1 rounded bg-black/70 hover:bg-rose-950 border border-rose-800 text-rose-300 font-bold transition"
          >
            [N] ABORT
          </button>
          <button
            type="button"
            onClick={() => onRespond(confirmation.requestId, true)}
            className="px-2.5 py-1 rounded bg-emerald-500 hover:bg-emerald-400 text-black font-bold transition"
          >
            [Y] PROCEED
          </button>
        </div>
      </div>
    </div>
  );
};
