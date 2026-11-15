"""Contrato único entre módulos do agente (Pydantic v2).

Tudo que vem do proxy ou do usuário é DADO não confiável: os modelos validam forma, nunca executam conteúdo.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# ── Domínio (respostas das tools) ─────────────────────────────────────────────

class Product(_Model):
    search_item_id: str
    name: str = ""
    store: str = ""
    price: Optional[float] = None
    currency_code: str = "BRL"
    currency_symbol: str = "R$"
    url: str = ""
    image_url: str = ""
    description: str = ""
    order: int = 0

    @property
    def id(self) -> str:
        return self.search_item_id


class Garment(_Model):
    garment_id: str
    ghost_image_url: str = ""
    type: str = ""
    category: str = ""
    status: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TryonResult(_Model):
    tryon_id: str
    look_id: str = ""
    garment_ids: list[str] = Field(default_factory=list)
    image_url: str = ""
    created_at: str = ""
    status: str = ""
    avatar_id: str = ""
    avatar_pose: str = "NORMAL"
    is_saved: bool = False


class LookItem(_Model):
    look_id: str
    garment_ids: list[str] = Field(default_factory=list)
    description: str = ""
    tryon_image_url: str = ""
    created_at: str = ""


class WardrobeItem(_Model):
    garment_id: str
    type: str = ""
    category: str = ""
    ghost_image_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def tags(self) -> list[str]:
        tags = self.metadata.get("tags") or []
        return [str(t) for t in tags] if isinstance(tags, list) else []


class AvatarPose(_Model):
    pose: str = "NORMAL"
    status: str = ""
    image_url: str = ""


class Avatar(_Model):
    avatar_id: str
    lifecycle_status: str = ""
    poses: list[AvatarPose] = Field(default_factory=list)


ArtifactType = Literal["PRODUCT", "GARMENT", "TRYON_CARD"]


# ── Requisição do turno ───────────────────────────────────────────────────────

class UserProfileBasic(_Model):
    name: str = ""
    gender: str = ""
    city: str = ""


class TurnRequest(_Model):
    user: str
    conversation_id: str
    profile: UserProfileBasic = Field(default_factory=UserProfileBasic)
    text: str = ""
    images: list[str] = Field(default_factory=list)

    @classmethod
    def from_openai(cls, body: dict[str, Any]) -> "TurnRequest":
        """Extrai o turno do payload OpenAI Chat Completions da Doris (só a mensagem atual)."""
        if not isinstance(body, dict):
            raise ValueError("payload inválido")
        metadata = body.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        user = str(body.get("user") or "").strip()
        conversation_id = str(metadata.get("conversation_id") or "").strip()
        if not user or not conversation_id:
            raise ValueError("payload sem 'user' ou 'metadata.conversation_id'")

        messages = body.get("messages") or []
        user_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
        content = user_msgs[-1].get("content") if user_msgs else ""

        texts: list[str] = []
        images: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif part.get("type") == "image_url":
                    img = part.get("image_url")
                    url = img.get("url") if isinstance(img, dict) else img
                    if isinstance(url, str) and url:
                        images.append(url)

        profile = metadata.get("profile") if isinstance(metadata.get("profile"), dict) else {}
        return cls(
            user=user,
            conversation_id=conversation_id,
            profile=UserProfileBasic.model_validate(profile),
            text="\n".join(t for t in texts if t).strip(),
            images=images,
        )


# ── Restrições e preferências ─────────────────────────────────────────────────

class ProductConstraints(_Model):
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    exclude_colors: list[str] = Field(default_factory=list)
    include_colors: list[str] = Field(default_factory=list)
    gender: Optional[str] = None


class UserPreferences(_Model):
    disliked_colors: list[str] = Field(default_factory=list)
    liked_colors: list[str] = Field(default_factory=list)
    size: Optional[str] = None
    styles: list[str] = Field(default_factory=list)
    budget_max: Optional[float] = None

    def is_empty(self) -> bool:
        return not (self.disliked_colors or self.liked_colors or self.size or self.styles
                    or self.budget_max is not None)
