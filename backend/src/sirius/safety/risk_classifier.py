"""RiskClassifier Middleware classifying tool calls into LOW, MEDIUM, HIGH_IRREVERSIBLE, or BLOCKED."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sirius.ipc.protocol import RiskLevel

_CAPTCHA_RE = re.compile(
    r"\b(captcha|recaptcha|hcaptcha|turnstile|altcha|verify\s+you\s+are\s+human|i\s+am\s+not\s+a\s+robot|anti[\s-]*bot)\b",
    re.IGNORECASE,
)

_SECRET_EXFIL_RE = re.compile(
    r"(/etc/shadow|\.ssh/id_(rsa|ed25519)|ipc\.token|\.vault\.enc|Login\s+Data|Cookies\.sqlite|GEMINI_API_KEY|GOOGLE_API_KEY)",
    re.IGNORECASE,
)

_DESTRUCTIVE_SHELL_RE = re.compile(
    r"(?:^|[;&|]\s*|\s)(rm\s+|rmdir\s+|shred\s+|mkfs|dd\s+if=|git\s+reset\s+--hard|git\s+push\s+--force|chmod\s+-R|chown\s+-R|sudo\s+|systemctl\s+(stop|disable|mask)|reg\s+delete|Remove-Item\b|Format-Volume\b|kill\s+-9)",
    re.IGNORECASE,
)

_FINANCIAL_OR_EXTERNAL_SEND_RE = re.compile(
    r"\b(pay\b|checkout|place\s+order|complete\s+purchase|buy\s+now|wire\s+transfer|confirm\s+payment|"
    r"send\s+email|send\s+message|post\s+tweet|publish\s+post|delete\s+account|delete\s+repository|"
    r"change\s+password|revoke\s+key|transfer\s+funds|\$\d+(\.\d{2})?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    reason: str
    spoken_confirmation_prompt: str = ""


class RiskClassifier:
    """Intercepts and classifies every tool invocation prior to execution."""

    LOW_RISK_TOOLS: frozenset[str] = frozenset(
        {
            "list_windows",
            "launch_app",
            "focus_window",
            "minimize_window",
            "tile_window",
            "get_volume",
            "set_volume",
            "set_mute",
            "set_brightness",
            "media_control",
            "send_notification",
            "get_clipboard",
            "set_clipboard",
            "search_files",
            "inspect_ui_hierarchy",
            "browser_list_tabs",
            "browser_open_url",
            "browser_switch_tab",
            "browser_distill_page",
            "browser_scroll",
            "recall_memory",
            "remember_fact",
            "save_voice_macro",
        }
    )

    def classify(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        tainted_by_untrusted_content: bool = False,
    ) -> RiskAssessment:
        serialized_args = json.dumps(arguments, default=str)

        # 1. Hard BLOCKED: CAPTCHA / anti-bot bypass or secret/credential exfiltration
        if _CAPTCHA_RE.search(serialized_args):
            return RiskAssessment(
                level=RiskLevel.BLOCKED,
                reason="CAPTCHA and anti-bot verification gates cannot be solved or bypassed autonomously; handing control to you.",
            )

        if _SECRET_EXFIL_RE.search(serialized_args):
            return RiskAssessment(
                level=RiskLevel.BLOCKED,
                reason="Access to protected credentials, SSH keys, or keychain vaults is blocked for security.",
            )

        # 2. Untrusted Prompt-Injection Escalation Defense
        if tainted_by_untrusted_content and tool_name in {
            "run_shell_command",
            "run_os_script",
            "organize_file",
        }:
            return RiskAssessment(
                level=RiskLevel.BLOCKED,
                reason="Blocked OS command/script execution because recent page content contained suspicious prompt-injection patterns.",
            )

        # 3. Shell & OS Script Inspection
        if tool_name in ("run_shell_command", "run_os_script"):
            cmd_text = str(arguments.get("command") or arguments.get("script") or "")
            if _DESTRUCTIVE_SHELL_RE.search(cmd_text):
                preview = cmd_text.strip()[:70]
                return RiskAssessment(
                    level=RiskLevel.HIGH_IRREVERSIBLE,
                    reason="Destructive or privileged shell/OS command detected.",
                    spoken_confirmation_prompt=f"I'm about to run a potentially destructive command ({preview})—should I proceed?",
                )
            return RiskAssessment(
                level=RiskLevel.MEDIUM,
                reason="Standard shell/script execution.",
            )

        # 4. File Overwrite / Organization
        if tool_name == "organize_file":
            if bool(arguments.get("overwrite", False)):
                dst = str(arguments.get("destination", "target file"))
                return RiskAssessment(
                    level=RiskLevel.HIGH_IRREVERSIBLE,
                    reason="Overwriting existing destination file.",
                    spoken_confirmation_prompt=f"I'm about to overwrite {dst}—should I go ahead?",
                )
            return RiskAssessment(level=RiskLevel.MEDIUM, reason="Moving file without overwrite.")

        # 5. UI & Browser Click / Submit Inspection (Payments, External Messages, Deletions, Credentials)
        if tool_name in ("interact_ui", "vision_click", "browser_click", "browser_fill"):
            label_context = " ".join(
                str(arguments.get(k, ""))
                for k in ("target_label", "target_description", "action_hint", "label_hint", "text")
            )
            if _FINANCIAL_OR_EXTERNAL_SEND_RE.search(label_context):
                desc = (
                    str(
                        arguments.get("action_hint")
                        or arguments.get("target_label")
                        or arguments.get("target_description")
                        or "this high-impact action"
                    )
                    .strip()[:80]
                )
                return RiskAssessment(
                    level=RiskLevel.HIGH_IRREVERSIBLE,
                    reason="Financial transaction, external message submission, or credential mutation.",
                    spoken_confirmation_prompt=f"I'm about to perform '{desc}'—should I proceed?",
                )

        # 6. Network radio toggles / closing apps
        if tool_name in ("set_wifi", "set_bluetooth", "close_window", "browser_close_tab"):
            return RiskAssessment(level=RiskLevel.MEDIUM, reason="Reversible session/radio state change.")

        if tool_name in self.LOW_RISK_TOOLS:
            return RiskAssessment(level=RiskLevel.LOW, reason="Safe read or reversible UI navigation.")

        # External MCP tools default to MEDIUM unless arguments match financial/destructive patterns
        if _FINANCIAL_OR_EXTERNAL_SEND_RE.search(serialized_args):
            return RiskAssessment(
                level=RiskLevel.HIGH_IRREVERSIBLE,
                reason="High-impact external tool action.",
                spoken_confirmation_prompt=f"I'm about to execute {tool_name}—should I proceed?",
            )

        return RiskAssessment(level=RiskLevel.MEDIUM, reason="Standard tool execution.")
