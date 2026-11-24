import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && Boolean(window.__TAURI_INTERNALS__);
}

export async function readLocalIpcToken(): Promise<string> {
  if (!isTauriRuntime()) {
    return localStorage.getItem("sirius_dev_ipc_token") ?? "";
  }
  try {
    return await invoke<string>("read_ipc_token");
  } catch {
    return "";
  }
}

export async function setClickThrough(enabled: boolean): Promise<void> {
  if (!isTauriRuntime()) return;
  try {
    await invoke("set_click_through", { enabled });
  } catch {
    // Non-fatal on platforms without cursor passthrough support
  }
}

export async function setDrawerWindowLayout(expanded: boolean): Promise<void> {
  if (!isTauriRuntime()) return;
  try {
    await invoke("set_drawer_expanded", { expanded });
  } catch {
    // Non-fatal
  }
}

export async function startWindowDrag(): Promise<void> {
  if (!isTauriRuntime()) return;
  try {
    await getCurrentWindow().startDragging();
  } catch {
    // Non-fatal
  }
}

export async function onNativeTrayEvent(
  eventName: string,
  handler: () => void
): Promise<() => void> {
  if (!isTauriRuntime()) {
    return () => undefined;
  }
  const unlisten = await listen(eventName, () => {
    handler();
  });
  return unlisten;
}
