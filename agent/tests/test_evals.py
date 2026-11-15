"""Testes unitários dos verificadores determinísticos de avaliação (`agent/evals/checks.py`)."""
from __future__ import annotations

from evals.checks import (
    check_forbidden_colors,
    check_gender_appropriateness,
    check_no_tools_called,
    check_price_grounding,
    check_price_limit,
    check_single_garment_tryon,
    check_tryon_shown,
    check_turn_done,
)
from evals.client import EvalTurnResult


def _make_turn(
    *,
    outcome: str = "done",
    total_ms: float = 4200.0,
    text: str = "Encontrei a Camisa por R$ 249,90.",
    events: list | None = None,
    calls: list | None = None,
) -> EvalTurnResult:
    evs = events if events is not None else [{"type": "text", "delta": text}, {"type": "done"}]
    c_list = calls if calls is not None else []
    t_log = {"outcome": outcome, "duration_ms": int(total_ms), "calls": c_list, "text": text}
    return EvalTurnResult(
        conversation_id="c1",
        user_id="u_ana",
        message="Me acha uma camisa até R$300",
        http_status=200,
        ttft_ms=800.0,
        total_ms=total_ms,
        events=evs,
        full_text=text,
        admin_log={"turns": [t_log]},
        turn_log=t_log,
    )


def test_check_turn_done():
    assert check_turn_done(_make_turn()).passed
    assert not check_turn_done(_make_turn(outcome="error")).passed
    assert not check_turn_done(_make_turn(total_ms=96000.0)).passed


def test_check_price_limit():
    ok_res = _make_turn(events=[
        {"type": "artifact", "artifact": {"kind": "PRODUCT", "item_id": "p01", "price": 249.9}},
        {"type": "done"},
    ])
    bad_res = _make_turn(events=[
        {"type": "artifact", "artifact": {"kind": "PRODUCT", "item_id": "p02", "price": 459.0}},
        {"type": "done"},
    ])
    assert check_price_limit(ok_res, 300.0).passed
    assert not check_price_limit(bad_res, 300.0).passed


def test_check_single_garment_tryon_and_tryon_shown():
    good_calls = [
        {"tool": "tryon", "status": 200, "request": {"garment_ids": ["g1"]},
         "response": {"ok": True, "data": {"tryon_id": "t1", "look_id": "l1"}}},
        {"tool": "show_artifact", "status": 200, "request": {"type": "TRYON_CARD", "value": "t1"},
         "response": {"ok": True}},
    ]
    res = _make_turn(calls=good_calls)
    assert check_single_garment_tryon(res).passed
    assert check_tryon_shown(res).passed

    bad_multi = _make_turn(calls=[
        {"tool": "tryon", "status": 400, "request": {"garment_ids": ["g1", "g2"]}, "response": {"ok": False}},
    ])
    assert not check_single_garment_tryon(bad_multi).passed

    unshown = _make_turn(calls=[good_calls[0]])
    assert not check_tryon_shown(unshown).passed


def test_check_forbidden_colors_and_gender():
    res_yellow = _make_turn(events=[
        {"type": "artifact", "artifact": {"kind": "PRODUCT", "item_id": "p25", "name": "Polo mostarda", "tags": ["mostarda"]}},
        {"type": "done"},
    ])
    assert not check_forbidden_colors(res_yellow, ["amarelo"]).passed

    res_fem = _make_turn(events=[
        {"type": "artifact", "artifact": {"kind": "PRODUCT", "item_id": "p27", "name": "Vestido midi", "tags": ["feminino"]}},
        {"type": "done"},
    ])
    assert not check_gender_appropriateness(res_fem, "MALE").passed
    assert check_gender_appropriateness(res_fem, "FEMALE").passed


def test_check_no_tools_and_price_grounding():
    assert check_no_tools_called(_make_turn(calls=[])).passed
    assert not check_no_tools_called(_make_turn(calls=[{"tool": "looks", "status": 200}])).passed

    grounded = _make_turn(
        text="A camisa custa R$ 249,90 (ou R$ 249.90).",
        events=[{"type": "artifact", "artifact": {"kind": "PRODUCT", "item_id": "p01", "price": 249.9}}, {"type": "done"}],
    )
    assert check_price_grounding(grounded).passed

    hallucinated = _make_turn(
        text="A camisa custa R$ 199,90.",
        events=[{"type": "artifact", "artifact": {"kind": "PRODUCT", "item_id": "p01", "price": 249.9}}, {"type": "done"}],
    )
    assert not check_price_grounding(hallucinated).passed
