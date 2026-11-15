"""Grafo LangGraph de orquestração determinística da Doris (ADR-001 a ADR-004)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from .allowlist import IdAllowlist
from .config import Settings
from .deadline import Deadline
from .filters import normalize
from .llm import LLMClient, LLMError
from .memory import (
    ConversationState,
    ConversationStore,
    ProfileStore,
    get_user_lock,
)
from .prompts import (
    DORIS_SYSTEM_BASE,
    format_product_for_llm,
    format_profile_context,
    sanitize_catalog_text,
)
from .proxy_client import ProxyClient
from .router import (
    RouteDecision,
    extract_preferences_heuristic,
    extract_preferences_llm,
    has_preference_signals,
    route_turn,
)
from .sse import TurnEmitter
from .tools import (
    TryonPipelineResult,
    find_products,
    get_latest_look,
    resolve_known_product,
    run_tryon_pipeline,
    show_products,
)
from .types import Product, TurnRequest, UserPreferences

log = logging.getLogger("doris.graph")


class ProductPick(BaseModel):
    chosen_id: str
    reason: str = ""


class OutfitPick(BaseModel):
    primary_id: str
    secondary_ids: list[str] = Field(default_factory=list)


class GraphState(TypedDict, total=False):
    req: TurnRequest
    settings: Settings
    proxy: ProxyClient
    llm: LLMClient
    conversations: ConversationStore
    profiles: ProfileStore
    emitter: TurnEmitter
    deadline: Deadline
    # Contexto carregado no turno
    prefs: UserPreferences
    history: list[dict[str, str]]
    conv_state: ConversationState
    allowlist: IdAllowlist
    decision: RouteDecision


def _fmt_price(price: Optional[float], symbol: str = "R$") -> str:
    if price is None:
        return "preço indisponível"
    return f"{symbol} {price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


async def _stream_or_fallback(
    llm: LLMClient,
    messages: list[dict[str, Any]],
    model: str,
    emitter: TurnEmitter,
    deadline: Deadline,
    fallback_text: str,
    *,
    max_tokens: int = 1500,
) -> None:
    if deadline.remaining() < 3.0:
        emitter.text(fallback_text)
        return
    emitted = False
    try:
        async for token in llm.stream_chat(
            messages, model=model, deadline=deadline, max_tokens=max(max_tokens, 1200)
        ):
            emitted = True
            emitter.text(token)
    except LLMError as e:
        log.warning("stream_chat falhou (%s); usando texto determinístico de fallback", e)
        if not emitted:
            emitter.text(fallback_text)
    if not emitted and not emitter.full_text.endswith(fallback_text):
        emitter.text(fallback_text)


def _rank_by_user_preferences(products: list[Product], prefs: UserPreferences) -> list[Product]:
    """Prioriza peças que batem com estilos ou cores favoritas do perfil sem descartar as demais."""
    if not products or (not prefs.styles and not prefs.liked_colors):
        return products

    def _score(p: Product) -> int:
        text = normalize(f"{p.name} {p.description}")
        s = 0
        for st in prefs.styles:
            if normalize(st) and normalize(st) in text:
                s += 3
        for col in prefs.liked_colors:
            if normalize(col) and normalize(col) in text:
                s += 2
        return s

    return sorted(products, key=_score, reverse=True)


async def _pick_best_product(
    candidates: list[Product],
    req: TurnRequest,
    prefs: UserPreferences,
    llm: LLMClient,
    model: str,
    deadline: Deadline,
) -> Product:
    ranked = _rank_by_user_preferences(candidates, prefs)
    if len(ranked) == 1 or deadline.remaining() < 30.0:
        return ranked[0]

    valid_ids = {p.search_item_id for p in ranked}
    catalog_json = json.dumps([format_product_for_llm(p) for p in ranked[:6]], ensure_ascii=False)
    messages = [
        {
            "role": "system",
            "content": (
                f"{DORIS_SYSTEM_BASE}\n"
                "Escolha o `chosen_id` (dentre os IDs fornecidos em <catalog_data>) que melhor atende ao pedido "
                "e ao perfil da pessoa."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Perfil: {format_profile_context(req.profile, prefs)}\n"
                f"Pedido: <user_message>{req.text}</user_message>\n"
                f"<catalog_data>\n{catalog_json}\n</catalog_data>"
            ),
        },
    ]
    try:
        pick = await llm.complete_json(messages, ProductPick, model=model, deadline=deadline)
        if pick.chosen_id in valid_ids:
            return next(p for p in ranked if p.search_item_id == pick.chosen_id)
    except LLMError as e:
        log.warning("pick_best_product LLM falhou (%s); usando 1º do ranking", e)
    return ranked[0]


def _select_complementary_outfit(candidates: list[Product], prefs: UserPreferences) -> tuple[Product, list[Product]]:
    """Seleciona 1 peça principal (para try-on) e até 2 peças secundárias complementares."""
    ranked = _rank_by_user_preferences(candidates, prefs)
    primary = ranked[0]
    p_norm = normalize(f"{primary.name} {primary.description}")
    is_dress = any(w in p_norm for w in ("vestido", "macacao"))
    is_bottom = any(w in p_norm for w in ("calca", "bermuda", "saia"))

    secondary: list[Product] = []
    for item in ranked[1:]:
        if item.search_item_id == primary.search_item_id:
            continue
        i_norm = normalize(f"{item.name} {item.description}")
        if is_dress:
            # Para vestido/macacão, prioriza sobreposição (blazer/jaqueta/casaco/cardigan)
            if any(w in i_norm for w in ("blazer", "jaqueta", "trench", "casaco", "colete")):
                secondary.append(item)
        elif is_bottom:
            # Se a principal é parte de baixo, prioriza parte de cima
            if any(w in i_norm for w in ("camisa", "camiseta", "blusa", "polo", "sueter", "blazer")):
                secondary.append(item)
        else:
            # Se a principal é parte de cima, prioriza parte de baixo ou terceira peça
            if any(w in i_norm for w in ("calca", "bermuda", "saia", "blazer", "jaqueta")):
                secondary.append(item)
        if len(secondary) >= 2:
            break

    if not secondary and len(ranked) > 1:
        secondary = [p for p in ranked[1:3] if p.search_item_id != primary.search_item_id]

    return primary, secondary[:2]


# ── Nós do Grafo ──────────────────────────────────────────────────────────────

async def node_load_context(state: GraphState) -> dict[str, Any]:
    req = state["req"]
    conversations = state["conversations"]
    profiles = state["profiles"]

    # Sincroniza com eventual extração em background do turno anterior do mesmo `user` (ADR-003)
    async with get_user_lock(req.user):
        prefs = await profiles.get(req.user)

    history = await conversations.get_messages(req.conversation_id, limit=16, user=req.user)
    conv_state = await conversations.get_state(req.conversation_id, user=req.user)
    allowlist = IdAllowlist.from_dict(conv_state.allowlist)
    for pid in conv_state.seen_products:
        allowlist.register("product", pid)

    await conversations.append_message(req.conversation_id, "user", req.text, user=req.user, images=req.images)
    return {
        "prefs": prefs,
        "history": history,
        "conv_state": conv_state,
        "allowlist": allowlist,
    }


async def node_route(state: GraphState) -> dict[str, Any]:
    decision = await route_turn(
        req=state["req"],
        history=state["history"],
        prefs=state["prefs"],
        seen_products=state["conv_state"].seen_products,
        llm=state["llm"],
        model=state["settings"].ROUTER_MODEL,
        deadline=state["deadline"],
    )
    log.info("route decision: intent=%s queries=%s constraints=%s",
             decision.intent, decision.search_queries, decision.constraints.model_dump())
    return {"decision": decision}


def _route_edge(state: GraphState) -> str:
    intent = state["decision"].intent
    if intent in ("smalltalk", "off_topic"):
        return "smalltalk_or_offtopic"
    return intent


async def node_smalltalk_or_offtopic(state: GraphState) -> dict[str, Any]:
    req = state["req"]
    decision = state["decision"]
    emitter = state["emitter"]
    llm = state["llm"]
    deadline = state["deadline"]
    name_part = f", {req.profile.name}" if req.profile.name else ""

    if decision.intent == "smalltalk":
        fallback = (
            f"Oi{name_part}! Tudo ótimo por aqui. Sou a Doris, sua consultora de moda. "
            "Me conta: está procurando alguma peça específica, montando um look para uma ocasião "
            "ou quer que eu avalie seu último look?"
        )
        prompt = (
            f"{DORIS_SYSTEM_BASE}\n"
            f"Cumprimente {req.profile.name or 'a pessoa'} de forma calorosa e breve (2 frases) e pergunte "
            "como pode ajudar com looks, busca de peças ou try-on hoje."
        )
    else:
        fallback = (
            f"Haha, boa pergunta{name_part}! Mas minha especialidade mesmo é moda e estilo pessoal. "
            "Se quiser buscar uma peça, montar um look para algum evento ou provar roupas no seu avatar, "
            "é só me dizer!"
        )
        prompt = (
            f"{DORIS_SYSTEM_BASE}\n"
            "A pessoa fez uma pergunta fora do universo de moda. Responda a curiosidade em no máximo 1 frase curta "
            "e bem-humorada e puxe imediatamente a conversa de volta para moda, looks ou try-on."
        )

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"<user_message>{req.text}</user_message>"},
    ]
    await _stream_or_fallback(llm, messages, state["settings"].MAIN_MODEL, emitter, deadline, fallback, max_tokens=200)
    return {}


async def node_preference_update(state: GraphState) -> dict[str, Any]:
    req = state["req"]
    emitter = state["emitter"]
    llm = state["llm"]
    deadline = state["deadline"]

    # Extrai já uma prévia determinística para compor uma resposta precisa e acolhedora
    extracted = extract_preferences_heuristic(req.text)
    merged_preview = state["prefs"]
    from .memory import merge_preferences
    merged_preview = merge_preferences(merged_preview, extracted)

    details: list[str] = []
    if merged_preview.disliked_colors:
        details.append(f"evitar tons de **{', '.join(merged_preview.disliked_colors)}**")
    if merged_preview.liked_colors:
        details.append(f"priorizar **{', '.join(merged_preview.liked_colors)}**")
    if merged_preview.styles:
        details.append(f"focar no estilo **{', '.join(merged_preview.styles)}**")
    if merged_preview.size:
        details.append(
            f"anotei que você veste **{merged_preview.size}** (nosso catálogo não filtra estoque por grade de tamanho, "
            "mas vou considerar isso ao comentar modelagem e caimento)"
        )
    summary_str = "; ".join(details) if details else "suas preferências de estilo"
    fallback = (
        f"Anotei tudo, {req.profile.name or 'pode deixar'}! Vou {summary_str} nas suas próximas recomendações. "
        "Quer que eu já sugira algum look ou busque uma peça para você?"
    )

    messages = [
        {
            "role": "system",
            "content": (
                f"{DORIS_SYSTEM_BASE}\n"
                "Confirme de forma simpática e concisa que você anotou as preferências declaradas pela pessoa "
                f"({format_profile_context(req.profile, merged_preview)}). "
                "Lembre-se de não prometer estoque por tamanho, pois o catálogo não tem grade de tamanhos."
            ),
        },
        {"role": "user", "content": f"<user_message>{req.text}</user_message>"},
    ]
    await _stream_or_fallback(llm, messages, state["settings"].MAIN_MODEL, emitter, deadline, fallback, max_tokens=220)
    return {"prefs": merged_preview}


async def _emit_tryon_commentary(
    chosen: Product,
    res: TryonPipelineResult,
    req: TurnRequest,
    prefs: UserPreferences,
    llm: LLMClient,
    model: str,
    emitter: TurnEmitter,
    deadline: Deadline,
) -> None:
    price_txt = _fmt_price(chosen.price, chosen.currency_symbol)
    safe_desc = sanitize_catalog_text(chosen.description)
    if res.status == "tryon_shown":
        fallback = (
            f"Prontinho! A **{chosen.name}** ({chosen.store}, {price_txt}) já está no seu avatar. "
            f"{safe_desc} O que achou do caimento?"
        )
        messages = [
            {
                "role": "system",
                "content": (
                    f"{DORIS_SYSTEM_BASE}\n"
                    "O try-on da peça acabou de ser exibido no avatar da pessoa. "
                    "Escreva um comentário curto (2 frases) elogiando o caimento e dando uma dica rápida de styling. "
                    f"Use apenas os dados reais da peça e cite o preço oficial ({price_txt})."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Perfil: {format_profile_context(req.profile, prefs)}\n"
                    f"Pedido: <user_message>{req.text}</user_message>\n"
                    f"<catalog_data>{json.dumps(format_product_for_llm(chosen), ensure_ascii=False)}</catalog_data>"
                ),
            },
        ]
        await _stream_or_fallback(llm, messages, model, emitter, deadline, fallback, max_tokens=220)
    elif res.status == "skipped_deadline":
        emitter.text(
            f"Como o tempo do turno ficou curto para renderizar o provador virtual com segurança, "
            f"exibi o card da **{chosen.name}** ({chosen.store}, {price_txt}) para você conferir agora. "
            "Se quiser que eu gere o try-on dela no seu avatar, é só me pedir!"
        )
    else:
        emitter.text(
            f"Tive uma instabilidade no provador virtual ao gerar a imagem no avatar agora, "
            f"mas já deixei o card da **{chosen.name}** ({chosen.store}, {price_txt}) na tela para você ver os detalhes!"
        )


async def node_search_and_tryon(state: GraphState) -> dict[str, Any]:
    req = state["req"]
    decision = state["decision"]
    prefs = state["prefs"]
    proxy = state["proxy"]
    conv_state = state["conv_state"]
    allowlist = state["allowlist"]
    emitter = state["emitter"]
    deadline = state["deadline"]
    llm = state["llm"]
    settings = state["settings"]

    outcome = await find_products(
        conversation_id=req.conversation_id,
        queries=decision.search_queries or [req.text],
        constraints=decision.constraints,
        user_prefs=prefs,
        gender=req.profile.gender,
        proxy=proxy,
        conv_state=conv_state,
        allowlist=allowlist,
        emitter=emitter,
        deadline=deadline,
    )

    if outcome.exact_match and outcome.products:
        chosen = await _pick_best_product(
            outcome.products, req, prefs, llm, settings.MAIN_MODEL, deadline
        )
        price_txt = _fmt_price(chosen.price, chosen.currency_symbol)
        # Primeiro token rápido ANTES do try-on!
        emitter.text(
            f"Encontrei a **{sanitize_catalog_text(chosen.name)}** ({sanitize_catalog_text(chosen.store)}) "
            f"por **{price_txt}**! Vou gerar o seu try-on agora...\n\n"
        )
        res = await run_tryon_pipeline(
            chosen.search_item_id,
            conversation_id=req.conversation_id,
            proxy=proxy,
            conv_state=conv_state,
            allowlist=allowlist,
            emitter=emitter,
            deadline=deadline,
        )
        await _emit_tryon_commentary(chosen, res, req, prefs, llm, settings.MAIN_MODEL, emitter, deadline)
        return {}

    # Não existe match exato (ex.: pediu "jaqueta jeans", mas só temos "Jaqueta de couro preta")
    alts = _rank_by_user_preferences(outcome.alternatives, prefs)[:2]
    if alts:
        await show_products(
            [p.search_item_id for p in alts],
            conversation_id=req.conversation_id,
            proxy=proxy,
            conv_state=conv_state,
            allowlist=allowlist,
            deadline=deadline,
        )
        alt_descriptions = ", ".join(
            f"**{sanitize_catalog_text(p.name)}** ({sanitize_catalog_text(p.store)}, {_fmt_price(p.price, p.currency_symbol)})"
            for p in alts
        )
        fallback = (
            f"Não encontrei exatamente o que você pediu no nosso catálogo agora, mas separei "
            f"{'esta alternativa que pode' if len(alts) == 1 else 'estas opções próximas que podem'} "
            f"combinar com você: {alt_descriptions}. Quer provar alguma delas no seu avatar?"
        )
        catalog_json = json.dumps([format_product_for_llm(p) for p in alts], ensure_ascii=False)
        messages = [
            {
                "role": "system",
                "content": (
                    f"{DORIS_SYSTEM_BASE}\n"
                    "O item exato que a pessoa pediu não existe no catálogo. Diga com transparência que não temos "
                    "essa peça exata no catálogo e apresente as alternativas reais mostradas nos cards "
                    "(<catalog_data>), citando nome exato e preço numérico oficial, e pergunte se ela quer provar alguma no avatar."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Pedido: <user_message>{req.text}</user_message>\n"
                    f"Alternativas exibidas: <catalog_data>{catalog_json}</catalog_data>"
                ),
            },
        ]
        await _stream_or_fallback(llm, messages, settings.MAIN_MODEL, emitter, deadline, fallback, max_tokens=250)
        return {}

    emitter.text(
        "Procurei no catálogo, mas não encontrei nenhuma peça que atenda a esses critérios agora. "
        "Quer tentar com outra cor, faixa de preço ou tipo de peça?"
    )
    return {}


async def node_tryon_known_item(state: GraphState) -> dict[str, Any]:
    req = state["req"]
    decision = state["decision"]
    prefs = state["prefs"]
    proxy = state["proxy"]
    conv_state = state["conv_state"]
    allowlist = state["allowlist"]
    emitter = state["emitter"]
    deadline = state["deadline"]
    llm = state["llm"]
    settings = state["settings"]

    ref_text = decision.referenced_item_text or req.text
    chosen = resolve_known_product(ref_text, conv_state.seen_products)

    if chosen is None:
        outcome = await find_products(
            conversation_id=req.conversation_id,
            queries=[ref_text],
            constraints=decision.constraints,
            user_prefs=prefs,
            gender=req.profile.gender,
            proxy=proxy,
            conv_state=conv_state,
            allowlist=allowlist,
            emitter=emitter,
            deadline=deadline,
        )
        pool = outcome.products or outcome.alternatives
        if pool:
            chosen = pool[0]

    if chosen is None:
        emitter.text(
            "Não consegui identificar qual peça você quer provar. Pode me dizer o nome dela para eu buscar no catálogo?"
        )
        return {}

    allowlist.register("product", chosen.search_item_id)
    price_txt = _fmt_price(chosen.price, chosen.currency_symbol)
    emitter.text(
        f"Boa escolha! Vou provar a **{sanitize_catalog_text(chosen.name)}** ({price_txt}) no seu avatar agora...\n\n"
    )
    res = await run_tryon_pipeline(
        chosen.search_item_id,
        conversation_id=req.conversation_id,
        proxy=proxy,
        conv_state=conv_state,
        allowlist=allowlist,
        emitter=emitter,
        deadline=deadline,
    )
    await _emit_tryon_commentary(chosen, res, req, prefs, llm, settings.MAIN_MODEL, emitter, deadline)
    return {}


async def node_outfit_for_occasion(state: GraphState) -> dict[str, Any]:
    req = state["req"]
    decision = state["decision"]
    prefs = state["prefs"]
    proxy = state["proxy"]
    conv_state = state["conv_state"]
    allowlist = state["allowlist"]
    emitter = state["emitter"]
    deadline = state["deadline"]
    llm = state["llm"]
    settings = state["settings"]

    queries = decision.search_queries or [req.text]
    outcome = await find_products(
        conversation_id=req.conversation_id,
        queries=queries,
        constraints=decision.constraints,
        user_prefs=prefs,
        gender=req.profile.gender,
        proxy=proxy,
        conv_state=conv_state,
        allowlist=allowlist,
        emitter=emitter,
        deadline=deadline,
    )
    pool = outcome.products or outcome.alternatives
    if not pool:
        emitter.text(
            "Não encontrei peças disponíveis que atendam às suas preferências para essa ocasião agora. "
            "Quer me contar se topa explorar outras cores ou estilos?"
        )
        return {}

    primary, secondaries = _select_complementary_outfit(pool, prefs)
    p_price = _fmt_price(primary.price, primary.currency_symbol)

    # Primeiro token rápido!
    emitter.text(
        f"Separei uma proposta especial para você! A peça principal é a **{sanitize_catalog_text(primary.name)}** "
        f"({sanitize_catalog_text(primary.store)}, **{p_price}**), que já vou provar no seu avatar...\n\n"
    )

    if secondaries:
        await show_products(
            [s.search_item_id for s in secondaries],
            conversation_id=req.conversation_id,
            proxy=proxy,
            conv_state=conv_state,
            allowlist=allowlist,
            deadline=deadline,
        )

    res = await run_tryon_pipeline(
        primary.search_item_id,
        conversation_id=req.conversation_id,
        proxy=proxy,
        conv_state=conv_state,
        allowlist=allowlist,
        emitter=emitter,
        deadline=deadline,
    )

    all_selected = [primary] + secondaries
    catalog_json = json.dumps([format_product_for_llm(p) for p in all_selected], ensure_ascii=False)
    sec_text = (
        " Também mostrei para compor o look: "
        + ", ".join(
            f"**{sanitize_catalog_text(s.name)}** ({_fmt_price(s.price, s.currency_symbol)})"
            for s in secondaries
        )
        + "."
        if secondaries
        else ""
    )
    status_note = (
        "O try-on da peça principal já está na tela!"
        if res.status == "tryon_shown"
        else "O provador virtual oscilou agora, então exibi os cards dos produtos para você ver os detalhes."
    )
    fallback = f"{status_note}{sec_text} Essa combinação equilibra conforto e elegância para a ocasião!"

    messages = [
        {
            "role": "system",
            "content": (
                f"{DORIS_SYSTEM_BASE}\n"
                f"Status do try-on da peça principal ({primary.name}): {res.status}.\n"
                "Explique em 2 a 3 frases curtas por que essas peças de <catalog_data> funcionam bem juntas "
                "para a ocasião e para o perfil da pessoa, citando os nomes e preços oficiais."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Perfil: {format_profile_context(req.profile, prefs)}\n"
                f"Ocasião/Pedido: <user_message>{req.text}</user_message>\n"
                f"Peças selecionadas: <catalog_data>{catalog_json}</catalog_data>"
            ),
        },
    ]
    await _stream_or_fallback(llm, messages, settings.MAIN_MODEL, emitter, deadline, fallback, max_tokens=280)
    return {}


async def node_opinion_on_look(state: GraphState) -> dict[str, Any]:
    req = state["req"]
    prefs = state["prefs"]
    proxy = state["proxy"]
    conv_state = state["conv_state"]
    allowlist = state["allowlist"]
    emitter = state["emitter"]
    deadline = state["deadline"]
    llm = state["llm"]
    settings = state["settings"]

    image_data_uri: Optional[str] = None
    look_desc = "Look enviado pela pessoa"

    if req.images:
        # Usuário anexou imagem(ns) no turno (já vem em data URI ou URL)
        try:
            image_data_uri = await proxy.fetch_image_as_data_uri(req.images[0], deadline)
        except Exception:  # noqa: BLE001
            image_data_uri = req.images[0]
    else:
        latest_data = await get_latest_look(
            conversation_id=req.conversation_id,
            proxy=proxy,
            conv_state=conv_state,
            allowlist=allowlist,
            emitter=emitter,
            deadline=deadline,
        )
        if latest_data.look is None:
            emitter.text(
                "Não encontrei nenhum look anterior salvo no seu perfil ainda! "
                "Que tal a gente buscar uma peça e gerar seu primeiro try-on?"
            )
            return {}
        look_desc = latest_data.look.description or f"Look {latest_data.look.look_id}"
        image_data_uri = latest_data.image_data_uri

    fallback = (
        f"Adorei analisar o seu look (**{look_desc}**)! O caimento valoriza muito a silhueta e traz uma proposta "
        "super versátil. Se quiser dar um toque extra, vale brincar com acessórios neutros ou uma terceira peça leve!"
    )

    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Perfil: {format_profile_context(req.profile, prefs)}\n"
                f"Descrição registrada do look: {look_desc}\n"
                f"Pergunta da pessoa: <user_message>{req.text}</user_message>\n"
                "Dê uma opinião consultiva de moda sobre este look (caimento, cores, ocasião ideal e 1 sugestão "
                "de styling), citando explicitamente a peça descrita."
            ),
        }
    ]
    if image_data_uri:
        user_content.append({"type": "image_url", "image_url": {"url": image_data_uri}})

    messages = [
        {"role": "system", "content": DORIS_SYSTEM_BASE},
        {"role": "user", "content": user_content},
    ]
    await _stream_or_fallback(llm, messages, settings.MAIN_MODEL, emitter, deadline, fallback, max_tokens=320)
    return {}


async def _background_extract_and_save(
    lock: asyncio.Lock,
    user: str,
    text: str,
    profiles: ProfileStore,
    llm: LLMClient,
    router_model: str,
) -> None:
    try:
        bg_deadline = Deadline(15.0)
        extracted = await extract_preferences_llm(text, llm, router_model, bg_deadline)
        if not extracted.is_empty():
            await profiles.merge(user, extracted)
    except Exception:  # noqa: BLE001
        log.exception("falha na extração de preferências em background para %s", user)
    finally:
        lock.release()


async def node_finalize(state: GraphState) -> dict[str, Any]:
    req = state["req"]
    conversations = state["conversations"]
    profiles = state["profiles"]
    conv_state = state["conv_state"]
    allowlist = state["allowlist"]
    emitter = state["emitter"]
    decision = state["decision"]

    conv_state.allowlist = allowlist.to_dict()
    await conversations.save_state(req.conversation_id, conv_state)
    await conversations.append_message(req.conversation_id, "assistant", emitter.full_text, user=req.user)

    # Dispara extração assíncrona em background segurando `get_user_lock(req.user)` desde já
    if decision.intent == "preference_update" or has_preference_signals(req.text):
        lock = get_user_lock(req.user)
        await lock.acquire()
        try:
            asyncio.create_task(
                _background_extract_and_save(
                    lock=lock,
                    user=req.user,
                    text=req.text,
                    profiles=profiles,
                    llm=state["llm"],
                    router_model=state["settings"].ROUTER_MODEL,
                )
            )
        except BaseException:
            lock.release()
            raise
    return {}


def build_doris_graph():
    builder = StateGraph(GraphState)
    builder.add_node("load_context", node_load_context)
    builder.add_node("route", node_route)
    builder.add_node("smalltalk_or_offtopic", node_smalltalk_or_offtopic)
    builder.add_node("preference_update", node_preference_update)
    builder.add_node("search_and_tryon", node_search_and_tryon)
    builder.add_node("tryon_known_item", node_tryon_known_item)
    builder.add_node("outfit_for_occasion", node_outfit_for_occasion)
    builder.add_node("opinion_on_look", node_opinion_on_look)
    builder.add_node("finalize", node_finalize)

    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "route")
    builder.add_conditional_edges(
        "route",
        _route_edge,
        {
            "smalltalk_or_offtopic": "smalltalk_or_offtopic",
            "preference_update": "preference_update",
            "search_and_tryon": "search_and_tryon",
            "tryon_known_item": "tryon_known_item",
            "outfit_for_occasion": "outfit_for_occasion",
            "opinion_on_look": "opinion_on_look",
        },
    )
    for node_name in (
        "smalltalk_or_offtopic",
        "preference_update",
        "search_and_tryon",
        "tryon_known_item",
        "outfit_for_occasion",
        "opinion_on_look",
    ):
        builder.add_edge(node_name, "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


_COMPILED_GRAPH = build_doris_graph()


async def run_doris_turn(
    req: TurnRequest,
    *,
    settings: Settings,
    proxy: ProxyClient,
    llm: LLMClient,
    conversations: ConversationStore,
    profiles: ProfileStore,
    emitter: TurnEmitter,
    deadline: Deadline,
) -> None:
    initial_state: GraphState = {
        "req": req,
        "settings": settings,
        "proxy": proxy,
        "llm": llm,
        "conversations": conversations,
        "profiles": profiles,
        "emitter": emitter,
        "deadline": deadline,
    }
    await _COMPILED_GRAPH.ainvoke(initial_state)
