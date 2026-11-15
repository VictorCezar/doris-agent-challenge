"""Allowlist de IDs por conversa (ADR-001): todo ID enviado ao proxy precisa ter sido visto nesta conversa
(vindo de uma resposta de tool), nunca inventado pelo LLM ou copiado de texto do usuário/catálogo."""
from __future__ import annotations

from typing import Iterable, Literal

IdKind = Literal["product", "garment", "tryon", "look"]
KINDS: tuple[IdKind, ...] = ("product", "garment", "tryon", "look")


class UnknownIdError(Exception):
    def __init__(self, kind: str, value: str):
        super().__init__(f"id desconhecido nesta conversa: {kind}={value!r}")
        self.kind = kind
        self.value = value


class IdAllowlist:
    def __init__(self) -> None:
        self._ids: dict[str, set[str]] = {k: set() for k in KINDS}

    def register(self, kind: IdKind, *ids: str) -> None:
        if kind not in self._ids:
            raise ValueError(f"tipo inválido: {kind}")
        self._ids[kind].update(i for i in ids if isinstance(i, str) and i)

    def register_many(self, kind: IdKind, ids: Iterable[str]) -> None:
        self.register(kind, *list(ids))

    def contains(self, kind: IdKind, value: str) -> bool:
        return value in self._ids.get(kind, set())

    def require(self, kind: IdKind | tuple[IdKind, ...], value: str) -> str:
        """Valida e devolve o ID; aceita um tipo ou uma tupla de tipos (ex.: TRYON_CARD aceita tryon ou look)."""
        kinds = (kind,) if isinstance(kind, str) else kind
        if isinstance(value, str) and any(self.contains(k, value) for k in kinds):
            return value
        raise UnknownIdError("|".join(kinds), str(value))

    def to_dict(self) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in self._ids.items()}

    @classmethod
    def from_dict(cls, data: dict | None) -> "IdAllowlist":
        out = cls()
        for kind, ids in (data or {}).items():
            if kind in out._ids and isinstance(ids, list):
                out.register(kind, *ids)
        return out
