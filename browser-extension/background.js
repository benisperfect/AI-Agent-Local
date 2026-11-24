// Sirius Companion Extension — Background Service Worker (Manifest V3)

let ws = null;
let connected = false;
let reconnectTimer = null;

async function getConfig() {
  const stored = await chrome.storage.local.get(["siriusHost", "siriusPort", "siriusToken"]);
  return {
    host: stored.siriusHost || "127.0.0.1",
    port: stored.siriusPort || 8765,
    token: stored.siriusToken || "",
  };
}

async function connectDaemon() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const cfg = await getConfig();
  if (!cfg.token) {
    return;
  }

  try {
    ws = new WebSocket(`ws://${cfg.host}:${cfg.port}`);
    ws.onopen = () => {
      ws.send(
        JSON.stringify({
          type: "auth",
          token: cfg.token,
          client_role: "browser_extension",
          metadata: { userAgent: navigator.userAgent },
        })
      );
    };

    ws.onmessage = async (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "auth_ok") {
          connected = true;
          chrome.storage.local.set({ siriusConnected: true });
          return;
        }
        if (msg.type === "extension_command") {
          await handleDaemonCommand(msg);
        }
      } catch (err) {
        console.error("[Sirius Extension] Message error:", err);
      }
    };

    ws.onclose = () => {
      connected = false;
      chrome.storage.local.set({ siriusConnected: false });
      scheduleReconnect();
    };

    ws.onerror = () => {
      connected = false;
    };
  } catch (err) {
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    connectDaemon();
  }, 3500);
}

async function resolveTargetTabId(requestedTabId) {
  if (typeof requestedTabId === "number") {
    return requestedTabId;
  }
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tabs.length > 0 && tabs[0].id !== undefined) {
    return tabs[0].id;
  }
  const allTabs = await chrome.tabs.query({});
  if (allTabs.length > 0 && allTabs[0].id !== undefined) {
    return allTabs[0].id;
  }
  throw new Error("No active browser tab found.");
}

async function handleDaemonCommand(cmd) {
  const { request_id, action, payload } = cmd;
  try {
    let data = {};
    if (action === "list_tabs") {
      const tabs = await chrome.tabs.query({});
      data = {
        tabs: tabs.map((t, idx) => ({
          tab_id: t.id,
          tab_index: idx,
          title: t.title || "",
          url: t.url || "",
          active: Boolean(t.active),
        })),
      };
    } else if (action === "open_tab") {
      const created = await chrome.tabs.create({
        url: payload.url,
        active: payload.active !== false,
      });
      data = { tab_id: created.id, url: created.pendingUrl || created.url || payload.url };
    } else if (action === "capture_visible_tab") {
      const dataUrl = await chrome.tabs.captureVisibleTab({ format: "png" });
      data = { data_url: dataUrl };
    } else {
      const tabId = await resolveTargetTabId(payload.tab_id);
      const response = await chrome.tabs.sendMessage(tabId, { action, payload });
      data = response || {};
    }

    ws.send(
      JSON.stringify({
        type: "extension_result",
        request_id,
        ok: true,
        data,
      })
    );
  } catch (err) {
    ws.send(
      JSON.stringify({
        type: "extension_result",
        request_id,
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      })
    );
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "sirius_reconnect") {
    if (ws) {
      ws.close();
      ws = null;
    }
    connectDaemon().then(() => sendResponse({ ok: true, connected }));
    return true;
  }
  if (message.type === "sirius_status") {
    sendResponse({ connected });
    return false;
  }
  return false;
});

connectDaemon();
