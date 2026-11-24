"""Encrypted SQLite + Cosine Vector Personal Memory Store (`cryptography.Fernet` + NumPy)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from cryptography.fernet import Fernet
from google import genai
from google.genai import types
from numpy.typing import NDArray

from sirius.ipc.protocol import MemoryEntryModel, VoiceMacroModel

EMBED_DIM = 256


def _local_ngram_embedding(text: str) -> NDArray[np.float32]:
    """Deterministic L2-normalized 256-dim signed feature-hashed word + char-3gram vector."""
    vec = np.zeros(EMBED_DIM, dtype=np.float32)
    cleaned = re.sub(r"\s+", " ", text.lower().strip())
    if not cleaned:
        return vec

    features: list[tuple[str, float]] = []
    words = re.findall(r"[a-z0-9_@.]+", cleaned)
    for w in words:
        features.append((f"w:{w}", 2.0))
        padded = f"<{w}>"
        for i in range(max(0, len(padded) - 2)):
            features.append((f"g:{padded[i : i + 3]}", 0.75))

    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % EMBED_DIM
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign * weight

    norm = float(np.linalg.norm(vec))
    if norm > 1e-8:
        vec /= norm
    return vec


class EncryptedMemoryStore:
    """Stores encrypted user preferences, aliases, favorite paths, and custom voice macros."""

    def __init__(
        self,
        db_path: Path,
        fernet_key: bytes,
        gemini_api_key: str | None = None,
        embedding_model: str = "gemini-embedding-2",
    ) -> None:
        self._db_path = db_path
        self._fernet = Fernet(fernet_key)
        self._api_key = gemini_api_key
        self._embedding_model = embedding_model
        self._init_schema_sync()

    def set_api_key(self, api_key: str | None) -> None:
        self._api_key = api_key

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    key_text TEXT NOT NULL,
                    encrypted_value BLOB NOT NULL,
                    embedding BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(category, key_text)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_macros (
                    macro_id TEXT PRIMARY KEY,
                    trigger_phrase TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    encrypted_steps BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def _decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(bytes(token)).decode("utf-8")

    async def _compute_embedding(self, text: str) -> NDArray[np.float32]:
        if self._api_key:
            try:
                client = genai.Client(api_key=self._api_key)
                resp = await client.aio.models.embed_content(
                    model=self._embedding_model,
                    contents=text,
                    config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
                )
                if resp.embeddings and resp.embeddings[0].values:
                    arr = np.asarray(resp.embeddings[0].values, dtype=np.float32)
                    norm = float(np.linalg.norm(arr))
                    if norm > 1e-8:
                        arr /= norm
                    return arr
            except Exception:
                pass
        return _local_ngram_embedding(text)

    async def upsert_memory(self, category: str, key: str, value: str) -> MemoryEntryModel:
        clean_cat = category.strip().lower() or "general"
        clean_key = key.strip()
        clean_val = value.strip()
        emb = await self._compute_embedding(f"{clean_cat} {clean_key}: {clean_val}")
        now = datetime.now(UTC).isoformat()

        def _write() -> MemoryEntryModel:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT memory_id FROM memories WHERE category = ? AND key_text = ?",
                    (clean_cat, clean_key),
                ).fetchone()
                mem_id = str(row[0]) if row else uuid.uuid4().hex[:12]
                conn.execute(
                    """
                    INSERT INTO memories (memory_id, category, key_text, encrypted_value, embedding, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category, key_text) DO UPDATE SET
                        encrypted_value = excluded.encrypted_value,
                        embedding = excluded.embedding,
                        updated_at = excluded.updated_at
                    """,
                    (
                        mem_id,
                        clean_cat,
                        clean_key,
                        self._encrypt(clean_val),
                        emb.tobytes(),
                        now,
                    ),
                )
            return MemoryEntryModel(
                memory_id=mem_id,
                category=clean_cat,
                key=clean_key,
                value=clean_val,
                updated_at=now,
            )

        return await asyncio.to_thread(_write)

    async def delete_memory(self, memory_id: str) -> bool:
        def _del() -> bool:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
                return cur.rowcount > 0

        return await asyncio.to_thread(_del)

    async def list_memories(self) -> list[MemoryEntryModel]:
        def _read_all() -> list[MemoryEntryModel]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT memory_id, category, key_text, encrypted_value, updated_at "
                    "FROM memories ORDER BY updated_at DESC"
                ).fetchall()
            return [
                MemoryEntryModel(
                    memory_id=str(r[0]),
                    category=str(r[1]),
                    key=str(r[2]),
                    value=self._decrypt(r[3]),
                    updated_at=str(r[4]),
                )
                for r in rows
            ]

        return await asyncio.to_thread(_read_all)

    async def search_semantic(self, query: str, top_k: int = 5) -> list[MemoryEntryModel]:
        q_vec = await self._compute_embedding(query)

        def _rank() -> list[MemoryEntryModel]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT memory_id, category, key_text, encrypted_value, embedding, updated_at FROM memories"
                ).fetchall()
            scored: list[tuple[float, MemoryEntryModel]] = []
            for r in rows:
                emb = np.frombuffer(r[4], dtype=np.float32)
                sim = float(np.dot(q_vec, emb)) if emb.size == q_vec.size else 0.0
                val = self._decrypt(r[3])
                # Exact keyword boost for aliases like "my manager"
                if str(r[2]).lower() in query.lower():
                    sim += 0.35
                scored.append(
                    (
                        sim,
                        MemoryEntryModel(
                            memory_id=str(r[0]),
                            category=str(r[1]),
                            key=str(r[2]),
                            value=val,
                            updated_at=str(r[5]),
                        ),
                    )
                )
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return [entry for score, entry in scored[:top_k] if score > 0.05]

        return await asyncio.to_thread(_rank)

    # ------------------------------------------------------------------
    # Custom Learned Voice Macros
    # ------------------------------------------------------------------

    async def upsert_macro(
        self,
        trigger_phrase: str,
        description: str,
        steps: list[str],
    ) -> VoiceMacroModel:
        norm_trigger = " ".join(trigger_phrase.lower().strip().split())
        now = datetime.now(UTC).isoformat()
        enc_steps = self._encrypt(json.dumps(steps))

        def _write() -> VoiceMacroModel:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT macro_id FROM voice_macros WHERE trigger_phrase = ?",
                    (norm_trigger,),
                ).fetchone()
                macro_id = str(row[0]) if row else uuid.uuid4().hex[:12]
                conn.execute(
                    """
                    INSERT INTO voice_macros (macro_id, trigger_phrase, description, encrypted_steps, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(trigger_phrase) DO UPDATE SET
                        description = excluded.description,
                        encrypted_steps = excluded.encrypted_steps,
                        updated_at = excluded.updated_at
                    """,
                    (macro_id, norm_trigger, description.strip(), enc_steps, now),
                )
            return VoiceMacroModel(
                macro_id=macro_id,
                trigger_phrase=norm_trigger,
                description=description.strip(),
                steps=steps,
                updated_at=now,
            )

        return await asyncio.to_thread(_write)

    async def delete_macro(self, macro_id: str) -> bool:
        def _del() -> bool:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM voice_macros WHERE macro_id = ?", (macro_id,))
                return cur.rowcount > 0

        return await asyncio.to_thread(_del)

    async def list_macros(self) -> list[VoiceMacroModel]:
        def _read() -> list[VoiceMacroModel]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT macro_id, trigger_phrase, description, encrypted_steps, updated_at "
                    "FROM voice_macros ORDER BY updated_at DESC"
                ).fetchall()
            out: list[VoiceMacroModel] = []
            for r in rows:
                steps_raw = json.loads(self._decrypt(r[3]))
                out.append(
                    VoiceMacroModel(
                        macro_id=str(r[0]),
                        trigger_phrase=str(r[1]),
                        description=str(r[2]),
                        steps=[str(s) for s in steps_raw],
                        updated_at=str(r[4]),
                    )
                )
            return out

        return await asyncio.to_thread(_read)
