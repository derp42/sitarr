import json

import pytest

from sitarr.config import Config, Server, Settings
from sitarr.db import Store
from sitarr.engine import Engine
from sitarr.models import Guidance, Item
from sitarr.sources import SourceUnavailable


def test_source_outage_keeps_prior_guidance_and_does_not_write(tmp_path, monkeypatch):
    config = Config(tmp_path)
    config.save(Settings(servers=[Server(id="home", url="http://jellyfin")], auto_apply=True))
    store = Store(tmp_path / "db")
    engine = Engine(config, store)
    item = Item("home", "1", "Jaws 2", 1978, "movie", official="PG", custom="PG-13")
    store.upsert(item)
    g = Guidance(
        13,
        "Common Sense Media",
        "https://www.commonsensemedia.org/movie-reviews/jaws-2",
        "exact identity",
        "Jaws 2",
        1978,
    )
    store.guidance(item.key, g)
    job = store.queue("resolve", {"key": item.key})

    def fail(*args, **kwargs):
        raise SourceUnavailable("Provider is unavailable")

    monkeypatch.setattr("sitarr.engine.Sources.lookup", fail)
    with pytest.raises(SourceUnavailable):
        engine.resolve(job, {"key": item.key})
    assert json.loads(store.get(item.key)["guidance"])["age"] == 13
    assert store.ledger() == []


def test_rejected_refresh_keeps_old_rating_for_review(tmp_path, monkeypatch):
    config = Config(tmp_path)
    store = Store(tmp_path / "db")
    engine = Engine(config, store)
    item = Item("home", "1", "Jaws 2", 1978, "movie", custom="PG-13")
    store.upsert(item)
    store.guidance(item.key, Guidance(13, "Common Sense Media", "", "exact", "Jaws 2", 1978))
    monkeypatch.setattr("sitarr.engine.Sources.lookup", lambda *a, **kw: None)
    job = store.queue("resolve", {"key": item.key})
    engine.resolve(job, {"key": item.key})
    row = store.get(item.key)
    assert row["status"] == "review" and json.loads(row["guidance"])["age"] == 13
    assert store.ledger() == []
