import asyncio

import pytest

from doris.memory import (IMAGE_PLACEHOLDER, ConversationState, ConversationStore, Database, ProfileStore,
                          get_user_lock)
from doris.types import UserPreferences


@pytest.fixture
def db(tmp_path):
    return Database.in_dir(tmp_path)


async def test_messages_replace_images_with_placeholder(db):
    store = ConversationStore(db)
    b64 = "data:image/jpeg;base64," + "A" * 5000
    await store.append_message("c1", "user", [{"type": "text", "text": "Gostou?"},
                                              {"type": "image_url", "image_url": {"url": b64}}], user="u_ana")
    await store.append_message("c1", "user", "olha", user="u_ana", images=[b64])
    await store.append_message("c1", "assistant", "Lindo!", user="u_ana")
    msgs = await store.get_messages("c1")
    assert msgs[0] == {"role": "user", "content": f"Gostou?\n{IMAGE_PLACEHOLDER}"}
    assert msgs[1]["content"] == f"olha\n{IMAGE_PLACEHOLDER}"
    assert msgs[2] == {"role": "assistant", "content": "Lindo!"}
    assert all("base64" not in m["content"] for m in msgs)
    assert await store.get_messages("c2") == []


async def test_messages_limit_keeps_most_recent(db):
    store = ConversationStore(db)
    for i in range(5):
        await store.append_message("c1", "user", f"m{i}")
    assert [m["content"] for m in await store.get_messages("c1", limit=2)] == ["m3", "m4"]


async def test_conversation_state_roundtrip(db):
    store = ConversationStore(db)
    assert await store.get_state("c1") == ConversationState()
    state = ConversationState(user_id="u_ana__a", seen_products={"p01": {"name": "Camisa"}}, garment_map={"p01": "g1"},
                              tryon_map={"g1": "t1"}, shown_artifacts=["TRYON_CARD:t1"],
                              allowlist={"product": ["p01"]})
    await store.save_state("c1", state)
    assert await store.get_state("c1", user="u_ana__a") == state
    # Se outro user (`u_ana__b`) consultar a mesma conversation_id após reset do proxy, não herda estado de `u_ana__a`
    assert await store.get_state("c1", user="u_ana__b") == ConversationState(user_id="u_ana__b")
    state.shown_artifacts.append("PRODUCT:p01")
    await store.save_state("c1", state)
    assert (await store.get_state("c1")).shown_artifacts == ["TRYON_CARD:t1", "PRODUCT:p01"]


async def test_profiles_isolated_by_full_user(db):
    profiles = ProfileStore(db)
    await profiles.merge("u_ana__1", UserPreferences(disliked_colors=["amarelo"], size="m", styles=["minimalista"]))
    p1 = await profiles.get("u_ana__1")
    p2 = await profiles.get("u_ana__2")
    assert p1.disliked_colors == ["amarelo"] and p1.size == "M" and p1.styles == ["minimalista"]
    assert p2 == UserPreferences() and p2.is_empty()
    assert (await profiles.get("u_ana")).is_empty()


async def test_profile_merge_semantics(db):
    profiles = ProfileStore(db)
    await profiles.merge("u", UserPreferences(disliked_colors=["Mostarda"], liked_colors=["preto"], budget_max=300))
    p = await profiles.merge("u", UserPreferences(liked_colors=["amarela", "branco"], styles=["Minimalista"]))
    assert p.liked_colors == ["preto", "amarelo", "branco"]
    assert p.disliked_colors == []  # mudou de ideia: o mais recente vence
    assert p.budget_max == 300 and p.styles == ["minimalista"]
    persisted = await ProfileStore(Database(db.path)).get("u")
    assert persisted == p


async def test_profile_persists_across_instances(tmp_path):
    await ProfileStore(Database.in_dir(tmp_path)).merge("u_bruno", UserPreferences(size="G"))
    assert (await ProfileStore(Database.in_dir(tmp_path)).get("u_bruno")).size == "G"


async def test_user_lock_per_full_user_and_serializes():
    assert get_user_lock("u_ana__1") is get_user_lock("u_ana__1")
    assert get_user_lock("u_ana__1") is not get_user_lock("u_ana__2")

    order: list[str] = []
    lock = get_user_lock("u_lock_test")

    async def writer():
        async with lock:
            await asyncio.sleep(0.05)
            order.append("write")

    async def reader():
        await asyncio.sleep(0.01)
        async with lock:
            order.append("read")

    await asyncio.gather(writer(), reader())
    assert order == ["write", "read"]
