"""Orquestradores determinísticos de alto nível (ADR-001 / ADR-004).

O LLM decide *o quê* buscar/vestir; estas funções executam *como*:
- `find_products`: busca paralela + deduplicação + filtros rígidos + allowlist + fallback amplo.
- `run_tryon_pipeline`: verificação de deadline (<22s) + cache de conversa + `garment_creation` -> `tryon` (1 peça)
  -> `show_artifact(TRYON_CARD)` + fallback transparente para `show_artifact(PRODUCT)` em erro.
- `show_products`: exibe cards `PRODUCT` validados pela `IdAllowlist`.
- `get_latest_look`: busca o último look da pessoa, exibe `TRYON_CARD` e converte imagem para data URI (base64).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal, Optional

from .allowlist import IdAllowlist
from .deadline import Deadline
from .filters import COLOR_SYNONYMS, apply_filters, canonical_color, normalize
from .memory import ConversationState
from .proxy_client import ProxyClient, ToolError
from .sse import TurnEmitter
from .types import LookItem, Product, ProductConstraints, UserPreferences

log = logging.getLogger("doris.tools")

MIN_TRYON_DEADLINE_S = 22.0

TryonStatus = Literal["tryon_shown", "fallback_product_shown", "skipped_deadline"]


@dataclass
class TryonPipelineResult:
    status: TryonStatus
    product_id: str
    garment_id: Optional[str] = None
    tryon_id: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class SearchOutcome:
    products: list[Product]
    exact_match: bool
    alternatives: list[Product]


_STOPWORDS = {
    "de", "da", "do", "das", "dos", "e", "a", "o", "as", "os", "um", "uma", "para", "pra", "com",
    "em", "no", "na", "por", "ate", "me", "acha", "quero", "tem", "algum", "alguma", "algo", "provar",
}

_GARMENT_NOUNS = {
    "camisa", "camiseta", "blusa", "vestido", "saia", "calca", "bermuda", "jaqueta",
    "blazer", "casaco", "trench", "macacao", "sueter", "trico", "colete", "polo", "cropped",
}


def merge_constraints_with_preferences(
    constraints: ProductConstraints,
    user_prefs: UserPreferences,
    gender: Optional[str],
) -> ProductConstraints:
    """Combina restrições do turno com preferências de longo prazo do usuário."""
    include_norm = {canonical_color(c) for c in constraints.include_colors if c}
    # Aversões de cor do perfil entram em exclude_colors, exceto se a pessoa pediu a cor explicitamente agora.
    pref_excluded = [c for c in user_prefs.disliked_colors if canonical_color(c) not in include_norm]
    exclude_colors = list(dict.fromkeys(
        [canonical_color(c) for c in (constraints.exclude_colors + pref_excluded) if c]
    ))
    include_colors = list(dict.fromkeys([canonical_color(c) for c in constraints.include_colors if c]))
    max_price = constraints.max_price if constraints.max_price is not None else user_prefs.budget_max
    return ProductConstraints(
        max_price=max_price,
        min_price=constraints.min_price,
        exclude_colors=exclude_colors,
        include_colors=include_colors,
        gender=constraints.gender or (gender or None),
    )


def _hard_constraints_only(c: ProductConstraints) -> ProductConstraints:
    """Mantém restrições inegociáveis (preço, cores odiadas/excluídas e gênero), relaxando `include_colors`."""
    return ProductConstraints(
        max_price=c.max_price,
        min_price=c.min_price,
        exclude_colors=list(c.exclude_colors),
        include_colors=[],
        gender=c.gender,
    )


def _query_tokens(q: str) -> list[str]:
    out: list[str] = []
    for t in normalize(q).split():
        if not t or t in _STOPWORDS or t.isdigit():
            continue
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        out.append(t)
    return out


def _matches_all_query_nouns(p: Product, query: str) -> bool:
    """Verifica se o produto casa com todos os termos descritivos da query (ex.: 'jaqueta' E 'jeans')."""
    q_tokens = _query_tokens(query)
    if not q_tokens:
        return True
    p_tokens = set(_query_tokens(f"{p.name} {p.description}"))
    for qt in q_tokens:
        # Expande sinônimos se for cor
        c_key = canonical_color(qt)
        if c_key in COLOR_SYNONYMS:
            syns = {_query_tokens(s)[0] for s in ([c_key] + COLOR_SYNONYMS[c_key]) if _query_tokens(s)}
            if not (p_tokens & syns):
                return False
        elif qt not in p_tokens:
            return False
    return True


def _broad_fallback_queries(queries: list[str]) -> list[str]:
    """Extrai substantivos principais (ex.: 'jaqueta', 'camisa') para 1 busca mais ampla."""
    broad: list[str] = []
    for q in queries:
        toks = _query_tokens(q)
        nouns = [t for t in toks if t in _GARMENT_NOUNS]
        candidate = nouns[0] if nouns else (toks[0] if toks else "")
        if candidate and candidate not in broad and candidate not in [ normalize(orig) for orig in queries ]:
            broad.append(candidate)
    return broad[:2]


async def _parallel_search(
    conversation_id: str,
    queries: list[str],
    proxy: ProxyClient,
    deadline: Deadline,
    limit_per_query: int = 8,
) -> list[Product]:
    clean_queries = [q.strip() for q in queries if q and q.strip()]
    if not clean_queries:
        return []
    tasks = [proxy.search_by_text(conversation_id, q, deadline, limit=limit_per_query) for q in clean_queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    seen_ids: set[str] = set()
    merged: list[Product] = []
    for res in results:
        if isinstance(res, Exception):
            log.warning("search_by_text falhou em uma das queries: %s", res)
            continue
        for p in res:
            if p.search_item_id not in seen_ids:
                seen_ids.add(p.search_item_id)
                merged.append(p)
    return merged


def _register_products(
    products: list[Product],
    conv_state: ConversationState,
    allowlist: IdAllowlist,
) -> None:
    for p in products:
        allowlist.register("product", p.search_item_id)
        conv_state.seen_products[p.search_item_id] = p.model_dump()


async def find_products(
    *,
    conversation_id: str,
    queries: list[str],
    constraints: ProductConstraints,
    user_prefs: UserPreferences,
    gender: Optional[str],
    proxy: ProxyClient,
    conv_state: ConversationState,
    allowlist: IdAllowlist,
    emitter: TurnEmitter,
    deadline: Deadline,
) -> SearchOutcome:
    """Busca peças em paralelo, deduplica, filtra deterministicamente e registra na allowlist."""
    merged_constraints = merge_constraints_with_preferences(constraints, user_prefs, gender)
    hard_constraints = _hard_constraints_only(merged_constraints)

    emitter.feedback("search-1", "Buscando peças no catálogo...", "running")
    raw_products = await _parallel_search(conversation_id, queries, proxy, deadline)

    # Filtra sempre pelas restrições rígidas antes de expor ao estado
    hard_valid = apply_filters(raw_products, hard_constraints)
    strict_valid = apply_filters(raw_products, merged_constraints)

    # Se veio vazio na busca estrita, tenta 1 busca mais ampla pelos substantivos principais
    if not strict_valid and not hard_valid:
        broad_queries = _broad_fallback_queries(queries)
        if broad_queries and deadline.remaining() > 5.0:
            more_raw = await _parallel_search(conversation_id, broad_queries, proxy, deadline)
            for p in more_raw:
                if all(p.search_item_id != x.search_item_id for x in raw_products):
                    raw_products.append(p)
            hard_valid = apply_filters(raw_products, hard_constraints)
            strict_valid = apply_filters(raw_products, merged_constraints)

    _register_products(hard_valid, conv_state, allowlist)

    # Verifica se os itens aprovados realmente batem com todos os termos pedidos (ex.: "jaqueta jeans")
    exact_matches = [
        p for p in strict_valid
        if any(_matches_all_query_nouns(p, q) for q in queries)
    ]

    if exact_matches:
        emitter.feedback("search-1", f"{len(exact_matches)} peça(s) encontrada(s)", "done")
        return SearchOutcome(products=exact_matches, exact_match=True, alternatives=hard_valid)

    if strict_valid and len(queries) > 1:
        # Múltiplas queries de ocasião (ex.: casamento na praia)
        emitter.feedback("search-1", f"{len(strict_valid)} peça(s) encontrada(s)", "done")
        return SearchOutcome(products=strict_valid, exact_match=True, alternatives=hard_valid)

    # Não houve match exato para uma peça específica (ex.: pediu "jaqueta jeans" e só há "jaqueta de couro preta")
    # Priorizamos alternativas que compartilhem o substantivo de peça (ex.: "jaqueta")
    requested_nouns = {t for q in queries for t in _query_tokens(q) if t in _GARMENT_NOUNS}
    if requested_nouns:
        noun_alts = [
            p for p in hard_valid
            if set(_query_tokens(f"{p.name} {p.description}")) & requested_nouns
        ]
        alternatives = noun_alts + [p for p in hard_valid if p not in noun_alts]
    else:
        alternatives = hard_valid

    count = len(alternatives)
    emitter.feedback(
        "search-1",
        f"{count} opção(ões) próxima(s) encontrada(s)" if count else "Nenhuma peça encontrada",
        "done",
    )
    return SearchOutcome(products=[], exact_match=False, alternatives=alternatives)


def resolve_known_product(
    referenced_text: Optional[str],
    seen_products: dict[str, dict],
) -> Optional[Product]:
    """Resolve uma referência textual (ex.: 'jaqueta de couro preta') contra `seen_products` da conversa."""
    if not seen_products:
        return None
    items = [Product.model_validate(d) for d in seen_products.values()]
    if not referenced_text or not referenced_text.strip():
        return items[-1]

    ref_tokens = set(_query_tokens(referenced_text))
    if not ref_tokens:
        return items[-1]

    best_score = 0
    best_item: Optional[Product] = None
    for p in reversed(items):  # mais recentes primeiro em caso de empate
        p_name_tokens = set(_query_tokens(p.name))
        p_desc_tokens = set(_query_tokens(p.description))
        score = 3 * len(ref_tokens & p_name_tokens) + len(ref_tokens & p_desc_tokens)
        if score > best_score:
            best_score = score
            best_item = p
    return best_item


def _grace_deadline(deadline: Deadline, min_seconds: float = 5.0) -> Deadline:
    """Garante tempo mínimo para `show_artifact` concluir mesmo se o `tryon` consumiu o fim do deadline."""
    return deadline if deadline.remaining() >= min_seconds else Deadline(min_seconds)


async def show_products(
    product_ids: list[str],
    *,
    conversation_id: str,
    proxy: ProxyClient,
    conv_state: ConversationState,
    allowlist: IdAllowlist,
    deadline: Deadline,
) -> list[str]:
    """Exibe cards `PRODUCT` validando cada ID contra a `IdAllowlist`."""
    shown: list[str] = []
    eff_deadline = _grace_deadline(deadline)
    for pid in product_ids:
        valid_id = allowlist.require("product", pid)
        try:
            await proxy.show_artifact(conversation_id, "PRODUCT", valid_id, eff_deadline)
            marker = f"PRODUCT:{valid_id}"
            if marker not in conv_state.shown_artifacts:
                conv_state.shown_artifacts.append(marker)
            shown.append(valid_id)
        except ToolError as e:
            log.warning("show_artifact(PRODUCT, %s) falhou: %s", valid_id, e)
    return shown


async def run_tryon_pipeline(
    product_id: str,
    *,
    conversation_id: str,
    proxy: ProxyClient,
    conv_state: ConversationState,
    allowlist: IdAllowlist,
    emitter: TurnEmitter,
    deadline: Deadline,
) -> TryonPipelineResult:
    """Executa `garment_creation` -> `tryon` (1 peça) -> `show_artifact(TRYON_CARD)` com fallback seguro."""
    valid_pid = allowlist.require("product", product_id)

    # 1. Verifica prazo restante (<22s não inicia try-on)
    if deadline.remaining() < MIN_TRYON_DEADLINE_S:
        log.warning("deadline curto (%.1fs < %.1fs); pulando try-on e mostrando PRODUCT %s",
                    deadline.remaining(), MIN_TRYON_DEADLINE_S, valid_pid)
        await show_products(
            [valid_pid],
            conversation_id=conversation_id,
            proxy=proxy,
            conv_state=conv_state,
            allowlist=allowlist,
            deadline=deadline,
        )
        return TryonPipelineResult(status="skipped_deadline", product_id=valid_pid)

    # 2. Garment creation (com cache da conversa)
    garment_id = conv_state.garment_map.get(valid_pid)
    if garment_id:
        allowlist.register("garment", garment_id)
    else:
        fb_garment_id = f"garment-{valid_pid}"
        emitter.feedback(fb_garment_id, "Preparando a peça...", "running")
        try:
            garment = await proxy.garment_creation(conversation_id, valid_pid, deadline)
            garment_id = garment.garment_id
            conv_state.garment_map[valid_pid] = garment_id
            allowlist.register("garment", garment_id)
            emitter.feedback(fb_garment_id, "Peça preparada", "done")
        except ToolError as e:
            log.warning("garment_creation falhou para %s: %s", valid_pid, e)
            emitter.feedback(fb_garment_id, "Não foi possível preparar a peça", "error")
            await show_products(
                [valid_pid],
                conversation_id=conversation_id,
                proxy=proxy,
                conv_state=conv_state,
                allowlist=allowlist,
                deadline=deadline,
            )
            return TryonPipelineResult(
                status="fallback_product_shown",
                product_id=valid_pid,
                error_message=e.message,
            )

    # 3. Try-on (SEMPRE 1 única peça, com cache da conversa)
    valid_gid = allowlist.require("garment", garment_id)
    tryon_id = conv_state.tryon_map.get(valid_gid)
    if tryon_id:
        allowlist.register("tryon", tryon_id)
    else:
        fb_tryon_id = f"tryon-{valid_pid}"
        emitter.feedback(fb_tryon_id, "Provando a peça no seu avatar...", "running")
        try:
            tryon_res = await proxy.tryon(conversation_id, valid_gid, deadline)
            tryon_id = tryon_res.tryon_id
            conv_state.tryon_map[valid_gid] = tryon_id
            allowlist.register("tryon", tryon_id)
            if tryon_res.look_id:
                allowlist.register("look", tryon_res.look_id)
            emitter.feedback(fb_tryon_id, "Try-on pronto!", "done")
        except ToolError as e:
            log.warning("tryon falhou para garment %s (product %s): %s", valid_gid, valid_pid, e)
            emitter.feedback(fb_tryon_id, "Não foi possível gerar o try-on agora", "error")
            await show_products(
                [valid_pid],
                conversation_id=conversation_id,
                proxy=proxy,
                conv_state=conv_state,
                allowlist=allowlist,
                deadline=deadline,
            )
            return TryonPipelineResult(
                status="fallback_product_shown",
                product_id=valid_pid,
                garment_id=valid_gid,
                error_message=e.message,
            )

    # 4. Exibe obrigatoriamente o TRYON_CARD (com grace deadline caso o tryon tenha estourado os 80s)
    valid_tid = allowlist.require(("tryon", "look"), tryon_id)
    show_dl = _grace_deadline(deadline)
    try:
        await proxy.show_artifact(conversation_id, "TRYON_CARD", valid_tid, show_dl)
        marker = f"TRYON_CARD:{valid_tid}"
        if marker not in conv_state.shown_artifacts:
            conv_state.shown_artifacts.append(marker)
        return TryonPipelineResult(
            status="tryon_shown",
            product_id=valid_pid,
            garment_id=valid_gid,
            tryon_id=valid_tid,
        )
    except ToolError as e:
        log.warning("show_artifact(TRYON_CARD, %s) falhou: %s", valid_tid, e)
        await show_products(
            [valid_pid],
            conversation_id=conversation_id,
            proxy=proxy,
            conv_state=conv_state,
            allowlist=allowlist,
            deadline=deadline,
        )
        return TryonPipelineResult(
            status="fallback_product_shown",
            product_id=valid_pid,
            garment_id=valid_gid,
            tryon_id=valid_tid,
            error_message=e.message,
        )


@dataclass
class LatestLookData:
    look: Optional[LookItem]
    image_data_uri: Optional[str]


async def get_latest_look(
    *,
    conversation_id: str,
    proxy: ProxyClient,
    conv_state: ConversationState,
    allowlist: IdAllowlist,
    emitter: TurnEmitter,
    deadline: Deadline,
) -> LatestLookData:
    """Busca o último look da pessoa, exibe `TRYON_CARD` e baixa a imagem em base64."""
    emitter.feedback("looks-1", "Buscando seu último look...", "running")
    try:
        looks = await proxy.looks(conversation_id, deadline, limit=1)
    except ToolError as e:
        log.warning("looks falhou: %s", e)
        emitter.feedback("looks-1", "Não consegui carregar seus looks", "error")
        return LatestLookData(look=None, image_data_uri=None)

    if not looks:
        emitter.feedback("looks-1", "Nenhum look encontrado", "done")
        return LatestLookData(look=None, image_data_uri=None)

    latest = looks[0]
    allowlist.register("look", latest.look_id)
    emitter.feedback("looks-1", "Look carregado", "done")

    try:
        await proxy.show_artifact(conversation_id, "TRYON_CARD", allowlist.require("look", latest.look_id), deadline)
        marker = f"TRYON_CARD:{latest.look_id}"
        if marker not in conv_state.shown_artifacts:
            conv_state.shown_artifacts.append(marker)
    except ToolError as e:
        log.warning("show_artifact(TRYON_CARD, %s) falhou: %s", latest.look_id, e)

    data_uri: Optional[str] = None
    if latest.tryon_image_url:
        try:
            data_uri = await proxy.fetch_image_as_data_uri(latest.tryon_image_url, deadline)
        except ToolError as e:
            log.warning("fetch_image_as_data_uri falhou para %s: %s", latest.tryon_image_url, e)

    return LatestLookData(look=latest, image_data_uri=data_uri)
