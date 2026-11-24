# Sirius — Cross-Platform Autonomous Voice-Controlled Computer & Web AI Agent

Sirius is an all-in-one, cross-platform (**Windows 10/11, macOS 13+, and Linux [X11 & Wayland]**) autonomous voice-driven desktop & web agent. Say **"Hey Sirius"** to enter continuous full-duplex conversation mode, interrupt Sirius mid-sentence at any time (**barge-in**), or say **"Stop!" / "Cancel!" / "Freeze!"** (or press `Ctrl+Shift+Space` / `Cmd+Shift+Space`) for a `<100ms` local emergency abort.

---

## Locked-In Environment Configuration
- **Custom Wake Word:** `"Hey Sirius"` (offline Vosk constrained-grammar + ONNX wake/kill-word spotter)
- **Cloud Intelligence Stack:** **Google Gemini 3.8 Live API** (`gemini-3.8-live` / `gemini-3.8-live-extended-thinking` with calm, warm `"Aoede"` voice, `NON_BLOCKING` async tools, Hybrid VAD, sliding-window compression, and session resumption) + **Gemini 4 Hydrogen** (`gemini-4-hydrogen` via GA `interactions.create` for multi-step planning, Set-of-Marks GUI visual grounding, and closed-loop verification) + **`gemini-embedding-2`**
- **Local Edge Fallback:** `Silero VAD` (ONNX), Frequency-Domain NLMS Acoustic Echo Cancellation (`AEC`), `faster-whisper` offline STT, and `Kokoro` / `Piper` / native OS TTS
- **Primary Browser:** **Brave Browser** via live **Playwright CDP** profile attachment (`127.0.0.1:9222`) + zero-restart **Manifest V3 Companion Extension** (`/browser-extension`)
- **Persona:** Calm, warm, composed executive assistant — auto-executes reversible actions silently and speaks a concise 1-sentence confirmation prompt before `HIGH_IRREVERSIBLE` actions

---

## Monorepo Layout

```text
sirius/
├── shared/               # Canonical IPC JSON Schema + strict TypeScript/Zod v3 definitions
├── backend/              # Python 3.12+ asyncio daemon (`uv` + `pyproject.toml`)
│   ├── src/sirius/
│   │   ├── audio/        # FD-NLMS AEC, Silero VAD, Vosk wake/kill spotter, Gemini Live, Whisper/TTS
│   │   ├── ipc/          # Authenticated localhost WebSocket hub (HMAC token verification)
│   │   ├── os_control/   # Tier-1 OS Adapters (Linux/macOS/Windows), Tier-2 A11y, Stateful Shell, MCP
│   │   ├── browser/      # Brave/Chrome/Edge/Firefox profile discovery, DOM distiller, CDP + Extension
│   │   ├── vision/       # Multi-monitor `mss` capture, Set-of-Marks CV annotator, Gemini Pro Vision
│   │   ├── safety/       # UntrustedContentGuard, RiskClassifier middleware, vocal SafetyGatekeeper
│   │   ├── memory/       # Fernet-encrypted SQLite + 256-d cosine semantic vectors + Voice Macros
│   │   └── agent/        # Unified ToolRegistry, AutonomousPlanner, ClosedLoopVerifier, Orchestrator
│   └── tests/            # 19 automated unit & live integration tests across Phases 1–5
├── browser-extension/    # Companion Manifest V3 extension for Brave / Chrome / Edge / Firefox
├── hud/                  # Tauri v2 (Rust + React 19 + TypeScript + Tailwind CSS v4) Floating HUD
└── scripts/              # Linux uinput/AT-SPI helper, systemd service, macOS launchd, Windows Task
```

---

## Quick Start

### 1. Configure Gemini API Key (Stored in Native OS Keychain)
```bash
cd backend
uv sync --extra dev
uv run sirius --set-api-key "YOUR_GEMINI_API_KEY"
uv run sirius --doctor
```

### 2. Start the Sirius Python Daemon
```bash
cd backend
uv run sirius --download-models
```

### 3. Attach Your Logged-In Brave Browser
- **Option A (Zero-Restart Extension):** Open `brave://extensions` -> enable **Developer mode** -> **Load unpacked** -> select `sirius/browser-extension`, then paste `~/.local/share/sirius/ipc.token` into the extension popup.
- **Option B (Direct Playwright CDP):** Launch Brave with `brave-browser --remote-debugging-port=9222`.

### 4. Launch the Floating Tauri v2 HUD
```bash
cd hud
pnpm install
pnpm tauri dev   # Native frameless transparent overlay (requires Rust cargo)
# Or run `pnpm dev` to preview the HUD at http://localhost:1420
```

### 5. Run Full Linter, Strict Typecheck & Verification Suite
```bash
make lint && make typecheck && make test && make hud-build
```
