"""Memória em duas camadas no SQLite (ADR-003).

- Curto prazo (`ConversationStore`): chaveada por `conversation_id`; mensagens + estado JSON da conversa.
  Imagens do usuário NUNCA são salvas em base64 — viram o marcador "[imagem anexada]".
- Longo prazo (`ProfileStore`): chaveada pelo `user` COMPLETO (`u_ana__abc` ≠ `u_ana__def`).
- `get_user_lock(user)`: lock em memória por `user` para serializar extração (background) e leitura do perfil.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel, Field

from .filters import canonical_color, normalize
from .types import UserPreferences

IMAGE_PLACEHOLDER = "[imagem anexada]"
DB_FILENAME = "doris_agent.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS conversation_state (
    conversation_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id TEXT PRIMARY KEY,
    preferences TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class ConversationState(BaseModel):
    user_id: str = ""
    seen_products: dict[str, dict] = Field(default_factory=dict)   # search_item_id -> Product (dump)
    garment_map: dict[str, str] = Field(default_factory=dict)      # search_item_id -> garment_id
    tryon_map: dict[str, str] = Field(default_factory=dict)        # garment_id -> tryon_id (mais recente)
    shown_artifacts: list[str] = Field(default_factory=list)       # "TYPE:value"
    allowlist: dict[str, list[str]] = Field(default_factory=dict)  # IdAllowlist.to_dict()


class Database:
    def __init__(self, path: Union[str, Path]):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = asyncio.Lock()
        self._ready = False

    @classmethod
    def in_dir(cls, data_dir: Union[str, Path]) -> "Database":
        return cls(os.path.join(str(data_dir), DB_FILENAME))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _run_sync(self, fn, *args):
        conn = self._connect()
        try:
            with conn:
                return fn(conn, *args)
        finally:
            conn.close()

    async def run(self, fn, *args):
        if not self._ready:
            async with self._init_lock:
                if not self._ready:
                    await asyncio.to_thread(self._run_sync, lambda c: c.executescript(_SCHEMA))
                    self._ready = True
        return await asyncio.to_thread(self._run_sync, fn, *args)


# ── curto prazo ───────────────────────────────────────────────────────────────

def sanitize_content(content: Any) -> str:
    """Converte conteúdo (str ou partes OpenAI) em texto persistível, trocando imagens por marcador."""
    if isinstance(content, str):
        return _strip_inline_data_uris(content)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                parts.append(IMAGE_PLACEHOLDER)
            elif part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(_strip_inline_data_uris(part["text"]))
        return "\n".join(p for p in parts if p)
    return ""


def _strip_inline_data_uris(text: str) -> str:
    import re
    return re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+", IMAGE_PLACEHOLDER, text)


class ConversationStore:
    def __init__(self, db: Database):
        self.db = db

    async def append_message(self, conversation_id: str, role: str, content: Any, *, user: str = "",
                             images: Optional[list[str]] = None) -> None:
        text = sanitize_content(content)
        if images:
            text = "\n".join(filter(None, [text] + [IMAGE_PLACEHOLDER] * len(images)))

        def _ins(c):
            c.execute("INSERT INTO messages(conversation_id,user_id,role,content,created_at) VALUES (?,?,?,?,?)",
                      (conversation_id, user, role, text, time.time()))
        await self.db.run(_ins)

    async def get_messages(self, conversation_id: str, limit: int = 20, *, user: str = "") -> list[dict[str, str]]:
        def _sel(c):
            if user:
                rows = c.execute(
                    "SELECT role, content FROM messages WHERE conversation_id=? AND (user_id=? OR user_id='') "
                    "ORDER BY id DESC LIMIT ?",
                    (conversation_id, user, limit),
                ).fetchall()
            else:
                rows = c.execute("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                                 (conversation_id, limit)).fetchall()
            return [{"role": r, "content": t} for r, t in reversed(rows)]
        return await self.db.run(_sel)

    async def get_state(self, conversation_id: str, *, user: str = "") -> ConversationState:
        def _sel(c):
            return c.execute("SELECT state FROM conversation_state WHERE conversation_id=?",
                             (conversation_id,)).fetchone()
        row = await self.db.run(_sel)
        if not row:
            return ConversationState(user_id=user)
        try:
            st = ConversationState.model_validate_json(row[0])
            if user and st.user_id and st.user_id != user:
                return ConversationState(user_id=user)
            if user and not st.user_id:
                st.user_id = user
            return st
        except ValueError:
            return ConversationState(user_id=user)

    async def save_state(self, conversation_id: str, state: ConversationState) -> None:
        payload = state.model_dump_json()

        def _up(c):
            c.execute("INSERT INTO conversation_state(conversation_id,state,updated_at) VALUES (?,?,?) "
                      "ON CONFLICT(conversation_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                      (conversation_id, payload, time.time()))
        await self.db.run(_up)


# ── longo prazo ───────────────────────────────────────────────────────────────

def _merge_list(base: list[str], extra: list[str]) -> list[str]:
    out = list(base)
    for item in extra:
        if item and item not in out:
            out.append(item)
    return out


def _norm_colors(colors: list[str]) -> list[str]:
    return _merge_list([], [canonical_color(c) for c in colors])


def merge_preferences(current: UserPreferences, update: UserPreferences) -> UserPreferences:
    """Merge determinístico: listas unidas sem duplicar; o mais recente vence em conflitos."""
    new_liked = _norm_colors(update.liked_colors)
    new_disliked = _norm_colors(update.disliked_colors)
    liked = [c for c in _merge_list(_norm_colors(current.liked_colors), new_liked) if c not in new_disliked]
    disliked = [c for c in _merge_list(_norm_colors(current.disliked_colors), new_disliked) if c not in new_liked]
    styles = _merge_list([normalize(s) for s in current.styles], [normalize(s) for s in update.styles])
    return UserPreferences(
        liked_colors=liked,
        disliked_colors=disliked,
        size=(update.size.strip().upper() if update.size and update.size.strip() else current.size),
        styles=[s for s in styles if s],
        budget_max=update.budget_max if update.budget_max is not None else current.budget_max,
    )


class ProfileStore:
    def __init__(self, db: Database):
        self.db = db

    async def get(self, user: str) -> UserPreferences:
        def _sel(c):
            return c.execute("SELECT preferences FROM user_profiles WHERE user_id=?", (user,)).fetchone()
        row = await self.db.run(_sel)
        if not row:
            return UserPreferences()
        try:
            return UserPreferences.model_validate(json.loads(row[0]))
        except ValueError:
            return UserPreferences()

    async def save(self, user: str, prefs: UserPreferences) -> None:
        payload = prefs.model_dump_json()

        def _up(c):
            c.execute("INSERT INTO user_profiles(user_id,preferences,updated_at) VALUES (?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET preferences=excluded.preferences, "
                      "updated_at=excluded.updated_at", (user, payload, time.time()))
        await self.db.run(_up)

    async def merge(self, user: str, update: UserPreferences) -> UserPreferences:
        merged = merge_preferences(await self.get(user), update)
        await self.save(user, merged)
        return merged


# ── locks por user ────────────────────────────────────────────────────────────

_user_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def get_user_lock(user: str) -> asyncio.Lock:
    """Lock por `user` COMPLETO (o sufixo `__abc` faz parte da identidade)."""
    return _user_locks[user]
