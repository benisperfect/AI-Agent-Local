"""Custom Voice Macro Matching & Natural Spoken Workflow Parser."""

from __future__ import annotations

import re

from sirius.ipc.protocol import VoiceMacroModel
from sirius.memory.store import EncryptedMemoryStore

_MACRO_DEF_RE = re.compile(
    r"^(?:whenever|when)\s+i\s+say\s+['\"]?([^'\",]+)['\"]?\s*,\s*(.+)$",
    re.IGNORECASE,
)


def parse_spoken_macro_definition(utterance: str) -> tuple[str, list[str]] | None:
    """Parse natural spoken macro setup e.g. \"Whenever I say 'start focus mode', close Slack, open VS Code, and play lo-fi\"."""
    m = _MACRO_DEF_RE.match(utterance.strip())
    if not m:
        return None
    trigger = " ".join(m.group(1).strip().lower().split())
    actions_blob = m.group(2).strip().rstrip(".")
    raw_steps = re.split(r"\s*(?:,\s*and\s+|,\s+|\s+and\s+then\s+|\s+then\s+)\s*", actions_blob)
    steps = [s.strip() for s in raw_steps if s.strip()]
    if not trigger or not steps:
        return None
    return (trigger, steps)


class VoiceMacroRegistry:
    """Resolves spoken trigger phrases into stored multi-step workflow instructions."""

    def __init__(self, store: EncryptedMemoryStore) -> None:
        self._store = store

    async def try_learn_from_utterance(self, utterance: str) -> VoiceMacroModel | None:
        parsed = parse_spoken_macro_definition(utterance)
        if parsed is None:
            return None
        trigger, steps = parsed
        return await self._store.upsert_macro(
            trigger_phrase=trigger,
            description=f"Custom voice macro for '{trigger}'",
            steps=steps,
        )

    async def match_utterance(self, utterance: str) -> VoiceMacroModel | None:
        norm = " ".join(
            utterance.lower().replace(".", " ").replace("!", " ").replace(",", " ").split()
        )
        macros = await self._store.list_macros()
        for macro in macros:
            if macro.trigger_phrase in norm:
                return macro
        return None
