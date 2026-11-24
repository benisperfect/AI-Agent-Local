import React, { useEffect, useState } from "react";
import { useSiriusSocket } from "./hooks/useSiriusSocket";
import { FloatingOrb } from "./components/FloatingOrb";
import { ConfirmationBanner } from "./components/ConfirmationBanner";
import { DrawerPanel } from "./components/DrawerPanel";
import { setClickThrough, setDrawerWindowLayout } from "./lib/tauriBridge";

export const App: React.FC = () => {
  const sirius = useSiriusSocket(8765);
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    const needsExpanded = drawerOpen || sirius.pendingConfirmation !== null;
    void setDrawerWindowLayout(needsExpanded);
    if (needsExpanded) {
      void setClickThrough(false);
    }
  }, [drawerOpen, sirius.pendingConfirmation]);

  return (
    <div
      className="w-full p-2"
      onMouseEnter={() => {
        void setClickThrough(false);
      }}
    >
      <FloatingOrb
        connected={sirius.connected}
        state={sirius.agentState}
        audio={sirius.audio}
        subtitle={sirius.subtitle}
        drawerOpen={drawerOpen}
        onToggleWake={() => {
          if (sirius.agentState === "SLEEPING") {
            sirius.wake();
          } else {
            sirius.sleep();
          }
        }}
        onEmergencyStop={sirius.emergencyStop}
        onToggleDrawer={() => setDrawerOpen((prev) => !prev)}
      />

      {sirius.pendingConfirmation && (
        <ConfirmationBanner
          confirmation={sirius.pendingConfirmation}
          onRespond={sirius.respondConfirmation}
        />
      )}

      {drawerOpen && (
        <DrawerPanel
          connected={sirius.connected}
          planGoal={sirius.planGoal}
          planSteps={sirius.planSteps}
          toolHistory={sirius.toolHistory}
          systemStatus={sirius.systemStatus}
          memories={sirius.memories}
          macros={sirius.macros}
          onSendTextCommand={sirius.sendTextCommand}
          onUpsertMemory={sirius.upsertMemory}
          onDeleteMemory={sirius.deleteMemory}
          onUpsertMacro={sirius.upsertMacro}
          onDeleteMacro={sirius.deleteMacro}
          onUpdateSettings={sirius.updateSettings}
          onSaveDevToken={sirius.saveDevToken}
        />
      )}
    </div>
  );
};
