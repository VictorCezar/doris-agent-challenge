"""Router estruturado de intenção e extrator de preferências (ADR-001 / ADR-002)."""
from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .deadline import Deadline
from .filters import COLOR_SYNONYMS, canonical_color, normalize
from .llm import LLMClient, LLMError
from .prompts import PREFERENCE_EXTRACTOR_PROMPT, ROUTER_SYSTEM_PROMPT, format_profile_context
from .types import ProductConstraints, TurnRequest, UserPreferences

log = logging.getLogger("doris.router")

IntentType = Literal[
    "search_and_tryon",
    "tryon_known_item",
    "outfit_for_occasion",
    "opinion_on_look",
    "preference_update",
    "smalltalk",
    "off_topic",
]


class RouteDecision(BaseModel):
    intent: IntentType
    search_queries: list[str] = Field(default_factory=list)
    constraints: ProductConstraints = Field(default_factory=ProductConstraints)
    referenced_item_text: Optional[str] = None
    occasion: Optional[str] = None


_GREETINGS = {"oi", "ola", "opa", "bom dia", "boa tarde", "boa noite", "tudo bem", "obrigado", "obrigada", "valeu"}
_PRICE_MAX_RE = re.compile(r"(?:ate|no maximo|abaixo de|menos de)\s*r?\$?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)


def _extract_deterministic_constraints(text: str, gender: Optional[str]) -> ProductConstraints:
    """Extrai restrições óbvias via regex/regras determinísticas para complementar o router LLM."""
    norm = normalize(text)
    max_price: Optional[float] = None
    m = _PRICE_MAX_RE.search(norm)
    if m:
        max_price = float(m.group(1).replace(",", "."))

    exclude_colors: list[str] = []
    for color_key, syns in COLOR_SYNONYMS.items():
        for syn in [color_key] + syns:
            syn_norm = normalize(syn)
            if re.search(rf"\b(?:odeio|nao gosto de|sem|exceto|nada)\s+(?:cor\s+)?{re.escape(syn_norm)}\b", norm):
                if color_key not in exclude_colors:
                    exclude_colors.append(color_key)

    return ProductConstraints(
        max_price=max_price,
        exclude_colors=exclude_colors,
        gender=gender or None,
    )


def merge_route_constraints(
    llm_constraints: ProductConstraints,
    det_constraints: ProductConstraints,
) -> ProductConstraints:
    max_price = llm_constraints.max_price if llm_constraints.max_price is not None else det_constraints.max_price
    exclude = list(dict.fromkeys(
        [canonical_color(c) for c in (llm_constraints.exclude_colors + det_constraints.exclude_colors) if c]
    ))
    include = list(dict.fromkeys(
        [canonical_color(c) for c in llm_constraints.include_colors if c]
    ))
    return ProductConstraints(
        max_price=max_price,
        min_price=llm_constraints.min_price,
        exclude_colors=exclude,
        include_colors=include,
        gender=llm_constraints.gender or det_constraints.gender,
    )


def _heuristic_route(req: TurnRequest, seen_products: dict[str, dict]) -> RouteDecision:
    """Fallback determinístico caso a chamada ao router LLM falhe."""
    text = req.text.strip()
    norm = normalize(text)
    det = _extract_deterministic_constraints(text, req.profile.gender)

    if not norm or norm in _GREETINGS:
        return RouteDecision(intent="smalltalk", constraints=det)

    if req.images or any(k in norm for k in ("ultimo look", "meu look", "o que voce acha", "o que acha do")):
        return RouteDecision(intent="opinion_on_look", constraints=det)

    if any(k in norm for k in ("provar", "experimentar", "vestir", "prova ")) and seen_products:
        return RouteDecision(intent="tryon_known_item", referenced_item_text=text, constraints=det)

    has_pref = any(k in norm for k in ("odeio", "nao gosto", "visto ", "meu tamanho", "curto estilo", "gosto de estilo"))
    has_request = any(k in norm for k in ("acha", "busca", "sugere", "mostra", "quero", "tem ", "visto?", "usar"))
    if has_pref and not has_request:
        return RouteDecision(intent="preference_update", constraints=det)

    if any(k in norm for k in ("casamento", "jantar", "festa", "trabalho", "formatura", "brunch", "encontro", "o que eu visto", "sugere algo")):
        queries = ["camisa linho", "calca linho", "bermuda linho"] if "praia" in norm else ["vestido", "blazer", "calca alfaiataria"]
        return RouteDecision(intent="outfit_for_occasion", search_queries=queries, occasion=text, constraints=det)

    if any(k in norm for k in ("capital da", "presidente", "receita de", "calcula", "quem descobriu")):
        return RouteDecision(intent="off_topic", constraints=det)

    return RouteDecision(intent="search_and_tryon", search_queries=[text], constraints=det)


async def route_turn(
    req: TurnRequest,
    history: list[dict[str, str]],
    prefs: UserPreferences,
    seen_products: dict[str, dict],
    llm: LLMClient,
    model: str,
    deadline: Deadline,
) -> RouteDecision:
    det = _extract_deterministic_constraints(req.text, req.profile.gender)
    norm = normalize(req.text)
    if norm in _GREETINGS and not req.images:
        return RouteDecision(intent="smalltalk", constraints=det)

    seen_summary = [
        f"{pid}: {p.get('name', '')} ({p.get('store', '')}, R$ {p.get('price', '')})"
        for pid, p in list(seen_products.items())[-10:]
    ]
    recent_hist = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
    user_context = (
        f"Perfil: {format_profile_context(req.profile, prefs)}\n"
        f"Produtos já vistos na conversa: {seen_summary or 'nenhum'}\n"
        f"Imagem anexada no turno: {'sim' if req.images else 'não'}\n"
        f"Histórico recente:\n{recent_hist or '(início da conversa)'}\n\n"
        f"<user_message>\n{req.text}\n</user_message>"
    )
    messages = [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": user_context},
    ]
    try:
        decision = await llm.complete_json(messages, RouteDecision, model=model, deadline=deadline)
    except LLMError as e:
        log.warning("router LLM falhou (%s); usando heurística determinística", e)
        return _heuristic_route(req, seen_products)

    decision.constraints = merge_route_constraints(decision.constraints, det)
    if decision.intent in ("search_and_tryon", "outfit_for_occasion") and not decision.search_queries:
        decision.search_queries = [req.text.strip()[:80]]
    decision.search_queries = [q.strip() for q in decision.search_queries if q and q.strip()][:3]
    return decision


def has_preference_signals(text: str) -> bool:
    """Detecta rápido se vale rodar extração de preferências em background."""
    norm = normalize(text)
    keywords = (
        "odeio", "detesto", "nao gosto", "evito", "prefiro", "amo", "adoro", "gosto de",
        "curto", "estilo", "minimalista", "classico", "casual", "elegante", "romantico",
        "visto ", "tamanho ", "meu manequim", "orcamento",
    )
    return any(k in norm for k in keywords)


def extract_preferences_heuristic(text: str) -> UserPreferences:
    """Extração determinística de segurança para preferências explícitas comuns."""
    norm = normalize(text)
    disliked: list[str] = []
    liked: list[str] = []
    for color_key, syns in COLOR_SYNONYMS.items():
        for syn in [color_key] + syns:
            s = normalize(syn)
            if re.search(rf"\b(?:odeio|detesto|nao gosto de|evito)\s+(?:cor\s+)?{re.escape(s)}\b", norm):
                if color_key not in disliked:
                    disliked.append(color_key)
            elif re.search(rf"\b(?:amo|adoro|gosto de|prefiro)\s+(?:cor\s+)?{re.escape(s)}\b", norm):
                if color_key not in liked:
                    liked.append(color_key)

    size: Optional[str] = None
    m_size = re.search(r"\b(?:visto|tamanho|manequim)\s+(pp|p|m|g|gg|xg|3[468]|4[0246])\b", norm)
    if m_size:
        size = m_size.group(1).upper()

    styles: list[str] = []
    for st in ("minimalista", "classico", "casual", "elegante", "romantico", "esportivo", "moderno", "urbano"):
        if re.search(rf"\b{st}\b", norm):
            styles.append(st)

    return UserPreferences(disliked_colors=disliked, liked_colors=liked, size=size, styles=styles)


async def extract_preferences_llm(
    text: str,
    llm: LLMClient,
    model: str,
    deadline: Deadline,
) -> UserPreferences:
    """Extrai preferências via LLM (`openai/gpt-6-luna`) combinando com a heurística determinística."""
    heur = extract_preferences_heuristic(text)
    messages = [
        {"role": "system", "content": PREFERENCE_EXTRACTOR_PROMPT},
        {"role": "user", "content": f"<user_message>\n{text}\n</user_message>"},
    ]
    try:
        llm_prefs = await llm.complete_json(messages, UserPreferences, model=model, deadline=deadline)
    except Exception as e:  # noqa: BLE001
        log.warning("extração de preferências via LLM falhou (%s); usando heurística", e)
        return heur

    from .memory import merge_preferences
    return merge_preferences(heur, llm_prefs)
