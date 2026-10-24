import json

import httpx
import pytest

from sitarr.config import Config, Server, Settings
from sitarr.db import Store
from sitarr.engine import Engine
from sitarr.models import Guidance, Item, managed_state
from sitarr.servers import Jellyfin, Plex


def jellyfin(initial=None):
    data = initial or {
        "Id": "123",
        "Name": "Jaws 2",
        "Type": "Movie",
        "ProductionYear": 1978,
        "OfficialRating": "PG",
        "CustomRating": None,
        "Tags": ["favorite"],
        "Overview": "Keep this",
        "ProviderIds": {"Tmdb": "579"},
        "Genres": ["Horror"],
        "LockedFields": [],
    }
    policy = {
        "IsAdministrator": False,
        "MaxParentalRating": 10,
        "BlockedTags": ["existing"],
        "EnableAllFolders": False,
        "EnabledFolders": ["movies"],
        "EnableContentDeletion": False,
    }

    def handler(r):
        if r.url.path.endswith("/Policy"):
            policy.update(json.loads(r.content))
            return httpx.Response(204)
        if r.url.path.startswith("/Users/"):
            return httpx.Response(200, json={"Policy": policy})
        if r.url.path == "/Items":
            if r.url.params.get("Ids"):
                assert "UserId" not in r.url.params
                return httpx.Response(200, json={"Items": [data], "TotalRecordCount": 1})
            return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})
        if r.method == "POST":
            data.update(json.loads(r.content))
            return httpx.Response(204)
        return httpx.Response(200, json=data)

    server = Jellyfin(
        Server(id="home", url="http://jellyfin", token="secret"),
        httpx.Client(base_url="http://jellyfin/", transport=httpx.MockTransport(handler)),
    )
    return server, data, policy


def test_jellyfin_changes_only_custom_rating_and_tags():
    server, data, _ = jellyfin()
    before = dict(data)
    server.write("123", {"custom": "PG-13", "tags": ["favorite", "Sitarr: Age 13+"]})
    assert {k: v for k, v in data.items() if k not in ["CustomRating", "Tags"]} == {
        k: v for k, v in before.items() if k not in ["CustomRating", "Tags"]
    }


def test_protection_preserves_permissions_and_stricter_existing_ceiling():
    server, _, policy = jellyfin()
    before = {k: v for k, v in policy.items() if k not in ["MaxParentalRating", "BlockedTags"]}
    result = server.protect("kid", 13, True)
    assert result["MaxParentalRating"] == 10
    assert "existing" in result["BlockedTags"]
    assert "Sitarr: Age 14+" in result["BlockedTags"]
    assert "Sitarr: Age 13+" not in result["BlockedTags"]
    assert "Sitarr: Unreviewed" in result["BlockedTags"]
    assert {k: v for k, v in policy.items() if k not in ["MaxParentalRating", "BlockedTags"]} == before


def engine_setup(tmp_path, monkeypatch, locked=False):
    config = Config(tmp_path)
    config.save(Settings(servers=[Server(id="home", url="http://jellyfin")]))
    store = Store(tmp_path / "db")
    engine = Engine(config, store)
    server, data, _ = jellyfin()
    if locked:
        data["LockedFields"] = ["Tags"]
    item = server.get("123")
    store.upsert(item)
    store.guidance(
        item.key,
        Guidance(13, "Common Sense Media", "https://example.test", "exact ID", item.title, item.year),
    )
    monkeypatch.setattr(server, "close", lambda: None)
    monkeypatch.setattr(engine, "server", lambda _: server)
    return engine, server, data, item


def test_ledger_and_undo_restore_originals(tmp_path, monkeypatch):
    engine, server, data, item = engine_setup(tmp_path, monkeypatch)
    engine.apply_one(item.key)
    assert data["CustomRating"] == "PG-13"
    changes = engine.store.ledger()
    assert len(changes) == 1 and changes[0]["status"] == "applied"
    engine.undo(changes[0]["id"])
    assert data["CustomRating"] is None and data["Tags"] == ["favorite"]
    assert engine.store.get(item.key)["status"] == "review"


def test_undo_refuses_to_destroy_later_edit(tmp_path, monkeypatch):
    engine, server, data, item = engine_setup(tmp_path, monkeypatch)
    engine.apply_one(item.key)
    data["CustomRating"] = "R"
    with pytest.raises(ValueError, match="edited after"):
        engine.undo(engine.store.ledger()[0]["id"])
    assert data["CustomRating"] == "R"


def test_locked_item_is_left_for_review(tmp_path, monkeypatch):
    engine, server, data, item = engine_setup(tmp_path, monkeypatch, True)
    engine.apply_one(item.key)
    assert data["CustomRating"] is None
    assert engine.store.get(item.key)["status"] == "review"
    assert engine.store.ledger() == []


def test_second_apply_writes_nothing(tmp_path, monkeypatch):
    engine, server, data, item = engine_setup(tmp_path, monkeypatch)
    engine.apply_one(item.key)
    engine.apply_one(item.key)
    assert len(engine.store.ledger()) == 1


def test_series_applies_to_children_and_preserves_stricter_episode(tmp_path, monkeypatch):
    config = Config(tmp_path)
    config.save(Settings(servers=[Server(id="home", url="http://jellyfin")]))
    store = Store(tmp_path / "db")
    engine = Engine(config, store)
    media = {
        "show": Item("home", "show", "Example Show", 2020, "series", official="TV-PG"),
        "season": Item("home", "season", "Season 1", 2020, "season", official="TV-PG"),
        "ep1": Item("home", "ep1", "Episode 1", 2020, "episode", official="TV-PG"),
        "ep2": Item("home", "ep2", "Episode 2", 2020, "episode", official="TV-MA"),
    }

    class FakeServer:
        def get(self, id):
            return media[id]

        def all_items(self, parent=None):
            return [media[k] for k in ["season", "ep1", "ep2"]]

        def write(self, id, state):
            media[id].custom, media[id].tags = state["custom"], state["tags"]
            return media[id]

        def close(self):
            pass

    monkeypatch.setattr(engine, "server", lambda _: FakeServer())
    store.upsert(media["show"])
    store.guidance(
        "home:show", Guidance(13, "Common Sense Media", "", "exact identity", "Example Show", 2020)
    )
    engine.apply_one("home:show")
    assert media["ep1"].custom == "TV-14"
    assert media["season"].custom == "TV-14"
    assert media["ep2"].custom == "TV-MA"
    assert all("Sitarr: Age 13+" in m.tags for m in media.values())
    assert len(store.ledger()) == 4


def test_uncertain_write_reconciles_after_remote_success(tmp_path, monkeypatch):
    engine, server, data, item = engine_setup(tmp_path, monkeypatch)
    engine.apply_one(item.key)
    with engine.store.connect() as c:
        c.execute("UPDATE changes SET status='uncertain'")
    engine.apply_one(item.key)
    assert engine.store.ledger()[0]["status"] == "applied"


def test_plex_ids_and_labels_and_verified_write():
    state = {"rating": "PG", "labels": ["favorite", "Sitarr: Age 8+"]}
    requests = []

    def xml():
        labels = "".join(f'<Label tag="{t}"/>' for t in state["labels"])
        return f'<MediaContainer><Video ratingKey="7" librarySectionID="1" type="movie" title="Jaws 2" year="1978" contentRating="{state["rating"]}"><Guid id="imdb://tt0077766"/>{labels}</Video></MediaContainer>'

    def handler(r):
        requests.append(r)
        if r.method == "PUT":
            state["rating"] = r.url.params["contentRating.value"]
            removed = r.url.params.get("label[].tag.tag-", "").split(",")
            state["labels"] = [x for x in state["labels"] if x not in removed]
            for k, v in r.url.params.items():
                if k.startswith("label[") and k.endswith("].tag.tag"):
                    state["labels"].append(v)
            return httpx.Response(200, text="<MediaContainer/>")
        return httpx.Response(200, text=xml())

    server = Plex(
        Server(id="plex", kind="plex", url="http://plex", token="secret"),
        httpx.Client(base_url="http://plex/", transport=httpx.MockTransport(handler)),
    )
    assert server.get("7").ids == {"imdb": "tt0077766"}
    new = {"custom": "PG-13", "tags": ["favorite", "Sitarr: Age 13+"]}
    assert managed_state(server.write("7", new)) == {"custom": "PG-13", "tags": sorted(new["tags"])}
    assert all("X-Plex-Token" not in str(r.url) for r in requests)
