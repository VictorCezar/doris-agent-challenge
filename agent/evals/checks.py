"""Verificadores determinísticos sobre o log do proxy (`/admin/log`), eventos SSE e texto final."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from doris.filters import color_terms, contains_any, is_feminine_only
from doris.types import Product

from .client import EvalTurnResult


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


_BRL_RE = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})+,\d{1,2}|\d+[.,]\d{1,2}|\d+)", re.IGNORECASE)


def _parse_brl_value(raw: str) -> float:
    s = raw.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def _catalog_items_seen_in_conv(res: EvalTurnResult) -> dict[str, dict[str, Any]]:
    """Coleta todos os produtos retornados por `search_by_text` ou `show_artifact` na conversa."""
    items: dict[str, dict[str, Any]] = {}
    for turn in res.admin_log.get("turns") or []:
        for call in turn.get("calls") or []:
            if call.get("tool") == "search_by_text" and call.get("status") == 200:
                prods = ((call.get("response") or {}).get("data") or {}).get("products") or []
                for p in prods:
                    pid = p.get("search_item_id") or p.get("id")
                    if pid:
                        items[pid] = dict(p)
    for ev in res.events:
        if ev.get("type") == "artifact":
            art = ev.get("artifact") or {}
            pid = art.get("item_id")
            if pid:
                merged = dict(items.get(pid, {}))
                merged.update(art)
                items[pid] = merged
    return items


def _shown_artifacts_in_turn(res: EvalTurnResult) -> list[dict[str, Any]]:
    return [ev.get("artifact") or {} for ev in res.events if ev.get("type") == "artifact"]


def _shown_item_ids_in_turn(res: EvalTurnResult) -> list[str]:
    out: list[str] = []
    for art in _shown_artifacts_in_turn(res):
        if art.get("item_id"):
            out.append(str(art["item_id"]))
        for iid in art.get("item_ids") or []:
            out.append(str(iid))
    return out


# ── Verificadores exigidos ────────────────────────────────────────────────────

def check_turn_done(res: EvalTurnResult, max_seconds: float = 95.0) -> CheckResult:
    outcome = res.turn_log.get("outcome")
    has_done_ev = any(ev.get("type") == "done" for ev in res.events)
    dur_ms = float(res.turn_log.get("duration_ms") or res.total_ms)
    limit_ms = max_seconds * 1000.0
    ok = outcome == "done" and has_done_ev and dur_ms < limit_ms and res.total_ms < limit_ms
    return CheckResult(
        name="turn_done",
        passed=ok,
        detail=f"outcome={outcome}, sse_done={has_done_ev}, duration_ms={dur_ms:.0f}ms (limit={limit_ms:.0f}ms)",
    )


def check_price_limit(res: EvalTurnResult, max_price: float) -> CheckResult:
    items = _catalog_items_seen_in_conv(res)
    violations: list[str] = []
    for art in _shown_artifacts_in_turn(res):
        price = art.get("price")
        if price is not None and float(price) > max_price:
            violations.append(f"{art.get('item_id')}(R${price})")
            continue
        for iid in ([art.get("item_id")] if art.get("item_id") else []) + list(art.get("item_ids") or []):
            info = items.get(str(iid)) or {}
            p_val = info.get("price")
            if p_val is not None and float(p_val) > max_price:
                violations.append(f"{iid}(R${p_val})")

    return CheckResult(
        name=f"price_limit(<={max_price:g})",
        passed=len(violations) == 0,
        detail="sem violações" if not violations else f"violações: {', '.join(violations)}",
    )


def check_single_garment_tryon(res: EvalTurnResult) -> CheckResult:
    calls = res.turn_log.get("calls") or []
    bad: list[str] = []
    for c in calls:
        if c.get("tool") == "tryon":
            gids = (c.get("request") or {}).get("garment_ids") or []
            status = c.get("status")
            if len(gids) != 1 or status == 400:
                bad.append(f"garment_ids={gids}, status={status}")
    return CheckResult(
        name="single_garment_tryon",
        passed=len(bad) == 0,
        detail="ok" if not bad else "; ".join(bad),
    )


def check_tryon_shown(res: EvalTurnResult) -> CheckResult:
    calls = res.turn_log.get("calls") or []
    created_tryons: list[tuple[str, str]] = []
    shown_tryon_values: set[str] = set()

    for c in calls:
        if c.get("tool") == "tryon" and c.get("status") == 200:
            data = (c.get("response") or {}).get("data") or {}
            tid = str(data.get("tryon_id") or "")
            lid = str(data.get("look_id") or "")
            if tid:
                created_tryons.append((tid, lid))
        elif c.get("tool") == "show_artifact" and c.get("status") == 200:
            req = c.get("request") or {}
            if req.get("type") == "TRYON_CARD" and req.get("value"):
                shown_tryon_values.add(str(req["value"]))

    missing = [tid for tid, lid in created_tryons if tid not in shown_tryon_values and lid not in shown_tryon_values]
    return CheckResult(
        name="tryon_shown",
        passed=len(missing) == 0,
        detail=f"criados={len(created_tryons)}, não mostrados={missing}",
    )


def check_forbidden_colors(res: EvalTurnResult, terms: list[str]) -> CheckResult:
    expanded: list[str] = []
    for t in terms:
        expanded.extend(color_terms(t) or [t])

    items = _catalog_items_seen_in_conv(res)
    violations: list[str] = []

    for art in _shown_artifacts_in_turn(res):
        tags_str = " ".join(str(t) for t in (art.get("tags") or []))
        name_str = str(art.get("name") or "")
        if contains_any(f"{name_str} {tags_str}", expanded):
            violations.append(f"artifact:{art.get('item_id') or art.get('tryon_id')}")
            continue
        for iid in _shown_item_ids_in_turn(res):
            info = items.get(iid) or {}
            blob = f"{info.get('name', '')} {info.get('description', '')} {' '.join(info.get('tags') or [])}"
            if contains_any(blob, expanded):
                violations.append(f"item:{iid}")

    return CheckResult(
        name=f"forbidden_colors({','.join(terms)})",
        passed=len(violations) == 0,
        detail="sem cores proibidas" if not violations else f"violações: {sorted(set(violations))}",
    )


def check_gender_appropriateness(res: EvalTurnResult, gender: str) -> CheckResult:
    if (gender or "").upper() != "MALE":
        return CheckResult(name="gender_appropriateness", passed=True, detail=f"gender={gender}")

    items = _catalog_items_seen_in_conv(res)
    violations: list[str] = []
    for art in _shown_artifacts_in_turn(res):
        tags = {str(t).lower() for t in (art.get("tags") or [])}
        if "feminino" in tags:
            violations.append(f"{art.get('item_id') or art.get('tryon_id')}(tag:feminino)")
    for iid in _shown_item_ids_in_turn(res):
        info = items.get(iid) or {}
        p = Product(
            search_item_id=iid,
            name=str(info.get("name") or ""),
            description=str(info.get("description") or ""),
        )
        if is_feminine_only(p):
            violations.append(f"{iid}({p.name})")

    return CheckResult(
        name="gender_appropriateness(MALE)",
        passed=len(violations) == 0,
        detail="ok" if not violations else f"itens femininos para MALE: {sorted(set(violations))}",
    )


def check_no_tools_called(res: EvalTurnResult) -> CheckResult:
    calls = res.turn_log.get("calls") or []
    tools = [c.get("tool") for c in calls]
    return CheckResult(
        name="no_tools_called",
        passed=len(tools) == 0,
        detail="0 tools" if not tools else f"tools chamadas: {tools}",
    )


def check_price_grounding(res: EvalTurnResult) -> CheckResult:
    items = _catalog_items_seen_in_conv(res)
    known_prices: set[float] = set()
    for info in items.values():
        if info.get("price") is not None:
            known_prices.add(round(float(info["price"]), 2))
    for art in _shown_artifacts_in_turn(res):
        if art.get("price") is not None:
            known_prices.add(round(float(art["price"]), 2))
    # Também aceita teto de preço citado pelo próprio usuário na pergunta se o agente o mencionar
    for m_user in _BRL_RE.finditer(res.message or ""):
        try:
            known_prices.add(round(_parse_brl_value(m_user.group(1)), 2))
        except ValueError:
            pass

    cited_raw = [m.group(1) for m in _BRL_RE.finditer(res.full_text or "")]
    ungrounded: list[str] = []
    for raw in cited_raw:
        try:
            val = round(_parse_brl_value(raw), 2)
        except ValueError:
            continue
        if not any(abs(val - kp) < 0.05 for kp in known_prices):
            ungrounded.append(f"R$ {raw}")

    return CheckResult(
        name="price_grounding",
        passed=len(ungrounded) == 0,
        detail=f"citados={cited_raw}, reais={sorted(known_prices)}" if not ungrounded
               else f"preços alucinados: {ungrounded} (reais={sorted(known_prices)})",
    )


def check_has_artifact(res: EvalTurnResult, kinds: list[str]) -> CheckResult:
    shown_kinds = [str(art.get("kind")) for art in _shown_artifacts_in_turn(res)]
    ok = any(k in shown_kinds for k in kinds)
    return CheckResult(
        name=f"has_artifact({'|'.join(kinds)})",
        passed=ok,
        detail=f"shown={shown_kinds}",
    )


def run_configured_checks(res: EvalTurnResult, check_specs: list[dict[str, Any]]) -> list[CheckResult]:
    """Executa a lista de checks configurada para um turno do `scenarios.yaml`."""
    results: list[CheckResult] = [check_turn_done(res)]
    for spec in check_specs:
        ctype = spec.get("type")
        if ctype == "price_limit":
            results.append(check_price_limit(res, float(spec["max_price"])))
        elif ctype == "single_garment_tryon":
            results.append(check_single_garment_tryon(res))
        elif ctype == "tryon_shown":
            results.append(check_tryon_shown(res))
        elif ctype == "forbidden_colors":
            results.append(check_forbidden_colors(res, list(spec.get("terms") or [])))
        elif ctype == "gender_appropriateness":
            results.append(check_gender_appropriateness(res, str(spec.get("gender") or "MALE")))
        elif ctype == "no_tools_called":
            results.append(check_no_tools_called(res))
        elif ctype == "price_grounding":
            results.append(check_price_grounding(res))
        elif ctype == "has_artifact":
            results.append(check_has_artifact(res, list(spec.get("kinds") or ["TRYON_CARD", "PRODUCT"])))
    return results
