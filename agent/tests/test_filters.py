import pytest

from doris.allowlist import IdAllowlist, UnknownIdError
from doris.filters import apply_filters, canonical_color, normalize
from doris.types import Product, ProductConstraints, TurnRequest


def P(id, name, price=100.0, description=""):
    return Product(search_item_id=id, name=name, price=price, description=description)


def ids(products):
    return [p.search_item_id for p in products]


def test_normalize():
    assert normalize("Camisa OFF-White Índigo, Açaí!") == "camisa off white indigo acai"


def test_polo_mostarda_rejected_when_excluding_amarelo():
    products = [P("p1", "Polo mostarda piquet"), P("p2", "Polo azul marinho"),
                P("p3", "Camiseta", description="Tom dourado suave")]
    out = apply_filters(products, ProductConstraints(exclude_colors=["amarelo"]))
    assert ids(out) == ["p2"]


def test_exclude_accepts_synonym_input():
    assert canonical_color("Amarela") == "amarelo"
    assert canonical_color("mostarda") == "amarelo"
    out = apply_filters([P("p1", "Polo amarela")], ProductConstraints(exclude_colors=["mostarda"]))
    assert out == []


def test_max_price_rejects_459_shirt():
    products = [P("p1", "Camisa de linho branca", 249.9), P("p2", "Camisa de linho premium", 459.0),
                P("p3", "Camisa sem preço", None)]
    out = apply_filters(products, ProductConstraints(max_price=300))
    assert ids(out) == ["p1"]


def test_min_price():
    out = apply_filters([P("a", "x", 50), P("b", "y", 150)], ProductConstraints(min_price=100))
    assert ids(out) == ["b"]


def test_include_colors_with_synonyms_and_accents():
    products = [P("p1", "Camisa de linho off-white"), P("p2", "Camisa cru"), P("p3", "Camisa preta"),
                P("p4", "Camisa", description="Linho BRANCA")]
    out = apply_filters(products, ProductConstraints(include_colors=["branco"]))
    assert ids(out) == ["p1", "p2", "p4"]


def test_word_boundary_avoids_false_positive():
    # "cru" não pode casar com "cruzado"; "top" não pode casar com "topázio"
    out = apply_filters([P("p1", "Bolsa transpassada cruzado")], ProductConstraints(exclude_colors=["branco"]))
    assert ids(out) == ["p1"]
    out = apply_filters([P("p2", "Anel topázio")], ProductConstraints(gender="MALE"))
    assert ids(out) == ["p2"]


def test_dress_rejected_for_male():
    products = [P("p1", "Vestido midi de linho"), P("p2", "Camisa de linho"), P("p3", "Saia plissada"),
                P("p4", "Bermuda", description="Modelagem feminina"), P("p5", "Cropped canelado")]
    assert ids(apply_filters(products, ProductConstraints(gender="MALE"))) == ["p2"]
    assert len(apply_filters(products, ProductConstraints(gender="FEMALE"))) == 5


def test_filters_preserve_order_and_combine():
    products = [P("p1", "Camisa branca", 200), P("p2", "Camisa amarela", 100), P("p3", "Vestido branco", 150),
                P("p4", "Camisa off white", 280)]
    c = ProductConstraints(max_price=300, exclude_colors=["amarelo"], include_colors=["branco"], gender="MALE")
    assert ids(apply_filters(products, c)) == ["p1", "p4"]


# ── allowlist ────────────────────────────────────────────────────────────────

def test_allowlist_validates_and_roundtrips():
    allow = IdAllowlist()
    allow.register("product", "p01", "p02")
    allow.register("garment", "g1")
    allow.register("look", "look_ana_1")
    assert allow.require("product", "p01") == "p01"
    assert allow.require(("tryon", "look"), "look_ana_1") == "look_ana_1"
    with pytest.raises(UnknownIdError):
        allow.require("product", "p99")
    with pytest.raises(UnknownIdError):
        allow.require("garment", "p01")  # ID de outro tipo não vale
    restored = IdAllowlist.from_dict(allow.to_dict())
    assert restored.require("garment", "g1") == "g1"


# ── TurnRequest ──────────────────────────────────────────────────────────────

def test_turn_request_from_openai():
    body = {"model": "doris-agent", "stream": True, "user": "u_ana__abc",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "O que acha?"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}]}],
            "metadata": {"conversation_id": "c1", "profile": {"name": "Ana", "gender": "FEMALE", "city": "SP"}}}
    req = TurnRequest.from_openai(body)
    assert req.user == "u_ana__abc" and req.conversation_id == "c1"
    assert req.text == "O que acha?" and req.images == ["data:image/jpeg;base64,AAA"]
    assert req.profile.name == "Ana" and req.profile.gender == "FEMALE"


def test_turn_request_requires_ids():
    with pytest.raises(ValueError):
        TurnRequest.from_openai({"messages": [], "metadata": {}})
