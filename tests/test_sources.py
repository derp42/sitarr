import httpx
import pytest
from test_guidance import page

from sitarr.config import Settings
from sitarr.db import Store
from sitarr.models import Item
from sitarr.sources import Sources, SourceUnavailable


def make(tmp_path, handler, **settings):
    source = Sources(
        Store(tmp_path / "db"), Settings(**settings), httpx.Client(transport=httpx.MockTransport(handler))
    )
    source.settings.request_delay = 0
    return source


def target():
    return Item("home", "1", "Jaws 2", 1978, "movie", ids={"imdb": "tt0077766"})


def test_uses_cached_age_without_repeating_network_lookup(tmp_path):
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(200, text=page())

    source = make(tmp_path, handler)
    assert source.lookup(target()).age == 13
    assert source.lookup(target()).age == 13
    assert len(calls) == 1


def test_remake_is_not_accepted_and_search_chooses_exact_title(tmp_path):
    def handler(r):
        if "autocomplete" in r.url.path:
            return httpx.Response(
                200, json=[{"items": [{"title": "Jaws 2", "url": "/movie-reviews/jaws-2-1978"}]}]
            )
        return httpx.Response(200, text=page(year="1978" if r.url.path.endswith("-1978") else "2025"))

    assert make(tmp_path, handler).lookup(target()).year == 1978


@pytest.mark.parametrize("code", [403, 429, 500])
def test_outage_not_cached_as_confirmed_no_match(tmp_path, code):
    source = make(tmp_path, lambda r: httpx.Response(code))
    with pytest.raises(SourceUnavailable):
        source.lookup(target())
    with source.store.connect() as c:
        assert c.execute("SELECT count(*) FROM cache").fetchone()[0] == 0


def test_mdblist_requires_matching_provider_id(tmp_path):
    source = make(
        tmp_path,
        lambda r: httpx.Response(
            200, json={"ids": {"imdb": "tt9999999"}, "commonsense": True, "age_rating": 5}
        ),
        mdblist_token="secret",
    )
    with pytest.raises(SourceUnavailable):
        source.lookup(target())


def test_mdblist_editorial_age_only(tmp_path):
    def handler(r):
        return httpx.Response(
            200, json={"ids": {"imdb": "tt0077766"}, "title": "Jaws 2", "commonsense": True, "age_rating": 13}
        )

    assert make(tmp_path, handler, mdblist_token="secret").lookup(target()).age == 13
