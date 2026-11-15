"""Filtros determinísticos sobre resultados de `search_by_text` (que não filtra preço/cor/gênero).

Casamento sem acento, case-insensitive, por palavra inteira, sobre `name + " " + description`.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Iterable

from .types import Product, ProductConstraints

COLOR_SYNONYMS: dict[str, list[str]] = {
    "amarelo": ["amarelo", "amarela", "mostarda", "dourado", "dourada", "ocre"],
    "branco": ["branco", "branca", "off-white", "off white", "cru"],
    "preto": ["preto", "preta"],
    "azul": ["azul", "marinho", "jeans", "indigo"],
    "vermelho": ["vermelho", "vermelha", "vinho", "bordo", "cereja"],
    "verde": ["verde", "oliva", "militar", "musgo"],
    "rosa": ["rosa", "pink", "rose"],
    "cinza": ["cinza", "grafite", "chumbo", "mescla"],
    "bege": ["bege", "areia", "nude", "caqui", "camel"],
    "marrom": ["marrom", "caramelo", "chocolate", "terracota", "cafe"],
    "laranja": ["laranja", "coral", "ferrugem"],
    "roxo": ["roxo", "roxa", "lilas", "lavanda", "violeta"],
}

FEMININE_ONLY_TERMS: list[str] = ["vestido", "saia", "blusa", "cropped", "top", "feminina", "feminino", "salto"]

_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Minúsculo, sem acentos, pontuação vira espaço (\"Off-White\" → \"off white\")."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    return _NON_WORD.sub(" ", ascii_text).strip()


@lru_cache(maxsize=512)
def _term_regex(term: str) -> re.Pattern:
    norm = normalize(term)
    # plural simples opcional ("pretos", "brancas")
    return re.compile(rf"\b{re.escape(norm)}s?\b")


def contains_any(text: str, terms: Iterable[str]) -> bool:
    norm = normalize(text)
    return any(_term_regex(t).search(norm) for t in terms if normalize(t))


def canonical_color(color: str) -> str:
    """Mapeia cor ou sinônimo para a chave canônica (\"mostarda\" → \"amarelo\"); desconhecida → normalizada."""
    norm = normalize(color)
    if not norm:
        return ""
    candidates = {norm, norm[:-1] if norm.endswith("s") else norm}
    for key, synonyms in COLOR_SYNONYMS.items():
        if candidates & ({key} | {normalize(s) for s in synonyms}):
            return key
    return norm


def color_terms(color: str) -> list[str]:
    key = canonical_color(color)
    if not key:
        return []
    return COLOR_SYNONYMS.get(key, [key])


def product_text(p: Product) -> str:
    return f"{p.name} {p.description}"


def matches_color(p: Product, color: str) -> bool:
    return contains_any(product_text(p), color_terms(color))


def is_feminine_only(p: Product) -> bool:
    return contains_any(product_text(p), FEMININE_ONLY_TERMS)


def _passes(p: Product, c: ProductConstraints) -> bool:
    if c.max_price is not None and (p.price is None or p.price > c.max_price):
        return False
    if c.min_price is not None and (p.price is None or p.price < c.min_price):
        return False
    if any(matches_color(p, color) for color in c.exclude_colors):
        return False
    include = [col for col in c.include_colors if canonical_color(col)]
    if include and not any(matches_color(p, color) for color in include):
        return False
    if (c.gender or "").strip().upper() == "MALE" and is_feminine_only(p):
        return False
    return True


def apply_filters(products: list[Product], constraints: ProductConstraints) -> list[Product]:
    """Mantém a ordem original da busca; remove o que viola as restrições."""
    return [p for p in products if _passes(p, constraints)]
