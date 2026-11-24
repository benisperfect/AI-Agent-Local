const statusBadge = document.getElementById("statusBadge");
const portInput = document.getElementById("portInput");
const tokenInput = document.getElementById("tokenInput");
const saveBtn = document.getElementById("saveBtn");

function renderStatus(connected) {
  statusBadge.textContent = connected ? "Connected" : "Disconnected";
  statusBadge.className = connected ? "badge online" : "badge";
}

chrome.storage.local.get(["siriusPort", "siriusToken", "siriusConnected"], (res) => {
  if (res.siriusPort) portInput.value = String(res.siriusPort);
  if (res.siriusToken) tokenInput.value = res.siriusToken;
  renderStatus(Boolean(res.siriusConnected));
});

chrome.runtime.sendMessage({ type: "sirius_status" }, (res) => {
  if (res) renderStatus(Boolean(res.connected));
});

saveBtn.addEventListener("click", () => {
  const port = parseInt(portInput.value, 10) || 8765;
  const token = tokenInput.value.trim();
  chrome.storage.local.set({ siriusPort: port, siriusToken: token }, () => {
    chrome.runtime.sendMessage({ type: "sirius_reconnect" }, (res) => {
      setTimeout(() => {
        chrome.runtime.sendMessage({ type: "sirius_status" }, (st) => {
          if (st) renderStatus(Boolean(st.connected));
        });
      }, 450);
    });
  });
});
