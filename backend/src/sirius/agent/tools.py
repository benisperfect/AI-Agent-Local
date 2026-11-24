"""Unified Tool Registry exposing Tier 1/2/3 OS, Shell, Browser, Memory, and MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from google.genai import types

from sirius.browser.agent import AuthenticatedBrowserAgent
from sirius.ipc.protocol import ControlTier, RiskLevel, ToolActivityMessage
from sirius.ipc.server import IPCServer
from sirius.memory.store import EncryptedMemoryStore
from sirius.os_control.base import MediaAction, ScriptLanguage, TilePosition
from sirius.os_control.controller import ComputerController
from sirius.safety.gatekeeper import SafetyGatekeeper
from sirius.safety.injection_guard import UntrustedContentGuard


@dataclass(frozen=True)
class ToolExecutionRecord:
    call_id: str
    tool_name: str
    tier: ControlTier
    risk_level: RiskLevel
    ok: bool
    summary: str
    payload: dict[str, Any]


class SiriusToolRegistry:
    """Single source of truth for Gemini tool declarations, safety gating, and execution."""

    def __init__(
        self,
        computer: ComputerController,
        browser: AuthenticatedBrowserAgent,
        memory: EncryptedMemoryStore,
        gatekeeper: SafetyGatekeeper,
        ipc: IPCServer,
        injection_guard: UntrustedContentGuard | None = None,
    ) -> None:
        self._computer = computer
        self._browser = browser
        self._memory = memory
        self._gatekeeper = gatekeeper
        self._ipc = ipc
        self._guard = injection_guard or UntrustedContentGuard()
        self._context_tainted: bool = False

    @property
    def context_tainted(self) -> bool:
        return self._context_tainted

    def reset_taint(self) -> None:
        self._context_tainted = False

    def gemini_declarations(self) -> list[types.FunctionDeclaration]:
        """Return complete FunctionDeclaration schemas for Gemini Live and Gemini 2.5 Pro."""
        decls = [
            types.FunctionDeclaration(
                name="launch_app",
                description="Launch any desktop application by name or executable path (Tier 1).",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "app_name": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["app_name"],
                },
            ),
            types.FunctionDeclaration(
                name="focus_window",
                description="Focus/activate an open desktop application window.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"title_or_app": {"type": "string"}},
                    "required": ["title_or_app"],
                },
            ),
            types.FunctionDeclaration(
                name="tile_window",
                description="Tile, maximize, or restore a desktop window (left, right, maximize, restore).",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "title_or_app": {"type": "string"},
                        "position": {
                            "type": "string",
                            "enum": ["left", "right", "maximize", "restore"],
                        },
                    },
                    "required": ["title_or_app", "position"],
                },
            ),
            types.FunctionDeclaration(
                name="close_window",
                description="Gracefully close a desktop application window.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"title_or_app": {"type": "string"}},
                    "required": ["title_or_app"],
                },
            ),
            types.FunctionDeclaration(
                name="set_volume",
                description="Set system speaker volume percentage (0-100).",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"percent": {"type": "integer"}},
                    "required": ["percent"],
                },
            ),
            types.FunctionDeclaration(
                name="set_mute",
                description="Mute or unmute system audio output.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"muted": {"type": "boolean"}},
                    "required": ["muted"],
                },
            ),
            types.FunctionDeclaration(
                name="set_brightness",
                description="Set display brightness percentage (5-100).",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"percent": {"type": "integer"}},
                    "required": ["percent"],
                },
            ),
            types.FunctionDeclaration(
                name="media_control",
                description="Control active media playback (play_pause, next, previous, stop).",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["play_pause", "next", "previous", "stop"],
                        }
                    },
                    "required": ["action"],
                },
            ),
            types.FunctionDeclaration(
                name="run_shell_command",
                description="Execute a command in Sirius's stateful terminal session.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                    "required": ["command"],
                },
            ),
            types.FunctionDeclaration(
                name="search_files",
                description="Search files/folders under a root directory by substring or glob.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "root": {"type": "string"},
                        "query": {"type": "string"},
                    },
                    "required": ["root", "query"],
                },
            ),
            types.FunctionDeclaration(
                name="organize_file",
                description="Move or rename a file/folder to a destination path.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "destination": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                    },
                    "required": ["source", "destination"],
                },
            ),
            types.FunctionDeclaration(
                name="inspect_ui_hierarchy",
                description="Inspect semantic Accessibility UI tree of the desktop or specific app (Tier 2).",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"app_or_window": {"type": "string"}},
                },
            ),
            types.FunctionDeclaration(
                name="interact_ui",
                description="Click or type into a desktop app control via Accessibility API with automatic Vision fallback (Tier 2 -> Tier 3).",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "target_label": {"type": "string"},
                        "app_or_window": {"type": "string"},
                        "action": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["target_label"],
                },
            ),
            types.FunctionDeclaration(
                name="browser_open_url",
                description="Open a URL in the user's authenticated browser (Brave by default) in foreground or silent background tab mode.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "foreground": {"type": "boolean"},
                    },
                    "required": ["url"],
                },
            ),
            types.FunctionDeclaration(
                name="browser_distill_page",
                description="Distill live authenticated webpage into compact interactive elements with data-agent-id selectors.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"tab_index": {"type": "integer"}},
                },
            ),
            types.FunctionDeclaration(
                name="browser_click",
                description="Click an interactive web element by its data-agent-id selector (include action_hint describing what the button does).",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "action_hint": {"type": "string"},
                        "tab_index": {"type": "integer"},
                    },
                    "required": ["agent_id"],
                },
            ),
            types.FunctionDeclaration(
                name="browser_fill",
                description="Fill text into a web input/textarea by data-agent-id selector.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "text": {"type": "string"},
                        "press_enter": {"type": "boolean"},
                        "action_hint": {"type": "string"},
                        "tab_index": {"type": "integer"},
                    },
                    "required": ["agent_id", "text"],
                },
            ),
            types.FunctionDeclaration(
                name="browser_select",
                description="Select an option in a <select> dropdown by data-agent-id.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "value_or_label": {"type": "string"},
                        "tab_index": {"type": "integer"},
                    },
                    "required": ["agent_id", "value_or_label"],
                },
            ),
            types.FunctionDeclaration(
                name="browser_upload",
                description="Upload a local file to a file input element by data-agent-id.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "file_path": {"type": "string"},
                        "tab_index": {"type": "integer"},
                    },
                    "required": ["agent_id", "file_path"],
                },
            ),
            types.FunctionDeclaration(
                name="browser_scroll",
                description="Scroll the active or specified browser tab vertically/horizontally.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "dx": {"type": "integer"},
                        "dy": {"type": "integer"},
                        "tab_index": {"type": "integer"},
                    },
                },
            ),
            types.FunctionDeclaration(
                name="remember_fact",
                description="Store a personal preference, contact alias, or favorite path in encrypted long-term memory.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["category", "key", "value"],
                },
            ),
            types.FunctionDeclaration(
                name="recall_memory",
                description="Search encrypted long-term personal memory semantically.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            types.FunctionDeclaration(
                name="save_voice_macro",
                description="Save a custom multi-step voice workflow macro triggered by a spoken phrase.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "trigger_phrase": {"type": "string"},
                        "description": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["trigger_phrase", "steps"],
                },
            ),
        ]

        # Append dynamically discovered MCP server tools
        for mcp_spec in self._computer.mcp.discovered_tools:
            decls.append(
                types.FunctionDeclaration(
                    name=mcp_spec.qualified_name,
                    description=mcp_spec.description,
                    parameters_json_schema=mcp_spec.input_schema,
                )
            )
        # Ensure NON_BLOCKING async execution for Gemini 3.8 Live & Extended Thinking
        for d in decls:
            d.behavior = types.Behavior.NON_BLOCKING
        return decls

    def infer_tier(self, tool_name: str) -> ControlTier:
        if tool_name.startswith("mcp__"):
            return ControlTier.MCP_TOOL
        if tool_name.startswith("browser_"):
            return (
                ControlTier.BROWSER_EXTENSION
                if self._browser.mode == "extension"
                else ControlTier.BROWSER_CDP
            )
        if tool_name in ("remember_fact", "recall_memory", "save_voice_macro"):
            return ControlTier.MEMORY
        if tool_name in ("inspect_ui_hierarchy", "interact_ui"):
            return ControlTier.TIER2_ACCESSIBILITY
        return ControlTier.TIER1_NATIVE

    async def execute_tool(
        self,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolExecutionRecord:
        """Run SafetyGatekeeper check -> dispatch tool -> emit HUD telemetry."""
        tier = self.infer_tier(tool_name)
        decision = await self._gatekeeper.evaluate_and_authorize(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            tainted_by_untrusted_content=self._context_tainted,
        )

        if not decision.allowed:
            status_lit: Literal["blocked", "cancelled"] = (
                "blocked" if decision.assessment.level == RiskLevel.BLOCKED else "cancelled"
            )
            await self._ipc.broadcast(
                ToolActivityMessage(
                    call_id=call_id,
                    tool_name=tool_name,
                    tier=tier,
                    risk_level=decision.assessment.level,
                    status=status_lit,
                    summary=decision.reason,
                ),
                roles={"hud", "cli"},
            )
            return ToolExecutionRecord(
                call_id=call_id,
                tool_name=tool_name,
                tier=tier,
                risk_level=decision.assessment.level,
                ok=False,
                summary=decision.reason,
                payload={"ok": False, "blocked_or_declined": True, "reason": decision.reason},
            )

        await self._ipc.broadcast(
            ToolActivityMessage(
                call_id=call_id,
                tool_name=tool_name,
                tier=tier,
                risk_level=decision.assessment.level,
                status="running",
                summary=f"Running {tool_name}",
            ),
            roles={"hud", "cli"},
        )

        try:
            actual_tier, summary, payload = await self._dispatch_raw(tool_name, arguments, tier)
            ok = bool(payload.get("ok", True))
        except Exception as exc:
            actual_tier = tier
            ok = False
            summary = f"{tool_name} failed: {exc}"
            payload = {"ok": False, "error": str(exc)}

        await self._ipc.broadcast(
            ToolActivityMessage(
                call_id=call_id,
                tool_name=tool_name,
                tier=actual_tier,
                risk_level=decision.assessment.level,
                status="succeeded" if ok else "failed",
                summary=summary,
            ),
            roles={"hud", "cli"},
        )
        return ToolExecutionRecord(
            call_id=call_id,
            tool_name=tool_name,
            tier=actual_tier,
            risk_level=decision.assessment.level,
            ok=ok,
            summary=summary,
            payload=payload,
        )

    async def _dispatch_raw(
        self,
        tool_name: str,
        args: dict[str, Any],
        default_tier: ControlTier,
    ) -> tuple[ControlTier, str, dict[str, Any]]:
        os_adp = self._computer.adapter

        if tool_name == "launch_app":
            msg = await os_adp.launch_app(
                str(args["app_name"]),
                [str(x) for x in args.get("args", [])] if args.get("args") else None,
            )
            return default_tier, msg, {"ok": True, "message": msg}

        if tool_name == "focus_window":
            ok = await os_adp.focus_window(str(args["title_or_app"]))
            return default_tier, f"Focused {args['title_or_app']}", {"ok": ok}

        if tool_name == "tile_window":
            pos: TilePosition = args["position"]
            ok = await os_adp.tile_window(str(args["title_or_app"]), pos)
            return default_tier, f"Tiled {args['title_or_app']} ({pos})", {"ok": ok}

        if tool_name == "close_window":
            ok = await os_adp.close_window(str(args["title_or_app"]))
            return default_tier, f"Closed {args['title_or_app']}", {"ok": ok}

        if tool_name == "set_volume":
            pct = int(args["percent"])
            ok = await os_adp.set_volume(pct)
            return default_tier, f"Set volume to {pct}%", {"ok": ok, "volume": pct}

        if tool_name == "set_mute":
            muted = bool(args["muted"])
            ok = await os_adp.set_mute(muted)
            return default_tier, f"Set mute={muted}", {"ok": ok, "muted": muted}

        if tool_name == "set_brightness":
            pct = int(args["percent"])
            ok = await os_adp.set_brightness(pct)
            return default_tier, f"Set brightness to {pct}%", {"ok": ok, "brightness": pct}

        if tool_name == "media_control":
            act: MediaAction = args["action"]
            ok = await os_adp.media_control(act)
            return default_tier, f"Media control: {act}", {"ok": ok}

        if tool_name == "run_shell_command":
            res = await self._computer.shell.execute(
                str(args["command"]),
                timeout_seconds=float(args.get("timeout_seconds", 30.0)),
            )
            return (
                default_tier,
                f"Shell exit={res.exit_code} in {res.cwd}",
                {
                    "ok": res.exit_code == 0 and not res.timed_out,
                    "exit_code": res.exit_code,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "cwd": res.cwd,
                },
            )

        if tool_name == "run_os_script":
            lang: ScriptLanguage = args["language"]
            out = await os_adp.run_os_script(str(args["script"]), lang)
            return default_tier, f"Executed {lang} OS script", {"ok": True, "output": out}

        if tool_name == "search_files":
            outcome = await self._computer.organize_or_search_files(
                "search", str(args["root"]), str(args["query"])
            )
            return outcome.tier_used, outcome.summary, {"ok": True, **outcome.data}

        if tool_name == "organize_file":
            outcome = await self._computer.organize_or_search_files(
                "move",
                str(args["source"]),
                str(args["destination"]),
                overwrite=bool(args.get("overwrite", False)),
            )
            return outcome.tier_used, outcome.summary, {"ok": True, **outcome.data}

        if tool_name == "inspect_ui_hierarchy":
            tree_txt = await self._computer.inspect_ui_hierarchy(
                app_or_window=str(args["app_or_window"]) if args.get("app_or_window") else None
            )
            return ControlTier.TIER2_ACCESSIBILITY, "Inspected UI tree", {"ok": True, "tree": tree_txt}

        if tool_name == "interact_ui":
            outcome = await self._computer.interact_ui(
                target_label=str(args["target_label"]),
                app_or_window=str(args["app_or_window"]) if args.get("app_or_window") else None,
                action=str(args.get("action", "click")),
                value=str(args["value"]) if args.get("value") is not None else None,
            )
            return outcome.tier_used, outcome.summary, {"ok": outcome.ok, **outcome.data}

        if tool_name == "browser_open_url":
            fg = bool(args.get("foreground", True))
            res_b = await self._browser.open_url(str(args["url"]), foreground=fg)
            return default_tier, f"Opened {args['url']} (foreground={fg})", res_b

        if tool_name == "browser_distill_page":
            tab_idx = int(args["tab_index"]) if args.get("tab_index") is not None else None
            distilled = await self._browser.distill_page(tab_index=tab_idx)
            sanitized = self._guard.isolate(distilled.to_llm_summary(), source=distilled.url or "webpage")
            if sanitized.suspicious:
                self._context_tainted = True
            return (
                default_tier,
                f"Distilled '{distilled.title}' ({len(distilled.elements)} elements)",
                {
                    "ok": True,
                    "url": distilled.url,
                    "title": distilled.title,
                    "needs_vision_fallback": distilled.needs_vision_fallback,
                    "untrusted_page_snapshot": sanitized.wrapped_text,
                    "injection_warning": sanitized.reasons,
                },
            )

        if tool_name == "browser_click":
            tab_idx = int(args["tab_index"]) if args.get("tab_index") is not None else None
            res_c = await self._browser.click(str(args["agent_id"]), tab_index=tab_idx)
            return default_tier, f"Clicked web element {args['agent_id']}", res_c

        if tool_name == "browser_fill":
            tab_idx = int(args["tab_index"]) if args.get("tab_index") is not None else None
            res_f = await self._browser.fill(
                str(args["agent_id"]),
                str(args["text"]),
                press_enter=bool(args.get("press_enter", False)),
                tab_index=tab_idx,
            )
            return default_tier, f"Filled web input {args['agent_id']}", res_f

        if tool_name == "browser_select":
            tab_idx = int(args["tab_index"]) if args.get("tab_index") is not None else None
            res_s = await self._browser.select_dropdown(
                str(args["agent_id"]),
                str(args["value_or_label"]),
                tab_index=tab_idx,
            )
            return default_tier, f"Selected dropdown {args['value_or_label']}", res_s

        if tool_name == "browser_upload":
            tab_idx = int(args["tab_index"]) if args.get("tab_index") is not None else None
            res_u = await self._browser.upload_file(
                str(args["agent_id"]),
                str(args["file_path"]),
                tab_index=tab_idx,
            )
            return default_tier, f"Uploaded file to {args['agent_id']}", res_u

        if tool_name == "browser_scroll":
            tab_idx = int(args["tab_index"]) if args.get("tab_index") is not None else None
            res_sc = await self._browser.scroll(
                dx=int(args.get("dx", 0)),
                dy=int(args.get("dy", 550)),
                tab_index=tab_idx,
            )
            return default_tier, "Scrolled browser page", res_sc

        if tool_name == "remember_fact":
            entry = await self._memory.upsert_memory(
                str(args["category"]),
                str(args["key"]),
                str(args["value"]),
            )
            return ControlTier.MEMORY, f"Remembered {entry.key}", {"ok": True, "entry": entry.model_dump()}

        if tool_name == "recall_memory":
            hits = await self._memory.search_semantic(str(args["query"]))
            return (
                ControlTier.MEMORY,
                f"Recalled {len(hits)} memory entries",
                {"ok": True, "memories": [h.model_dump() for h in hits]},
            )

        if tool_name == "save_voice_macro":
            macro = await self._memory.upsert_macro(
                str(args["trigger_phrase"]),
                str(args.get("description", "Custom voice workflow")),
                [str(s) for s in args["steps"]],
            )
            return ControlTier.MEMORY, f"Saved macro '{macro.trigger_phrase}'", {"ok": True, "macro": macro.model_dump()}

        if tool_name.startswith("mcp__"):
            res_m = await self._computer.mcp.invoke_tool(tool_name, args)
            return ControlTier.MCP_TOOL, f"Executed {tool_name}", res_m

        raise KeyError(f"Tool '{tool_name}' is not registered.")
