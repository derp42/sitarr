import json

import pytest

from sitarr.models import Guidance, Item, mapped_rating, proposal
from sitarr.sources import parse_review, valid_review_url


def item(**kwargs):
    return Item("home", "123", "Jaws 2", 1978, "movie", **kwargs)


def page(title="Jaws 2", year="1978", age=13, review_type="csm_movie"):
    props = {
        "csm_review_target_title": title,
        "csm_title_release_date": year,
        "csm_review_rating_age": age,
        "csm_review_type": review_type,
        "csm_review_rating_details_violence": 4,
    }
    return (
        '<script data-drupal-selector="drupal-settings-json" type="application/json">'
        + json.dumps({"csm": {"amplitude_props": props}})
        + "</script>"
    )


URL = "https://www.commonsensemedia.org/movie-reviews/jaws-2"


def test_exact_review_matches_and_keeps_source():
    g = parse_review(page(), URL, item())
    assert g.age == 13
    assert g.year == 1978
    assert g.details == {"violence": 4}
    assert g.url == URL


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "Jaws"},
        {"title": "Jaws 3"},
        {"year": "2025"},
        {"year": None},
        {"age": None},
        {"age": 99},
        {"review_type": "csm_tv"},
    ],
)
def test_ambiguous_or_wrong_identity_never_matches(changes):
    assert parse_review(page(**changes), URL, item()) is None


def test_does_not_confuse_review_publication_date_with_release_year():
    html = (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "Movie",
                "name": "Jaws 2",
                "dateCreated": "1978-01-01",
                "audience": {"suggestedMinAge": 13},
            }
        )
        + "</script>"
    )
    assert parse_review(html, URL, item()) is None


def test_ignores_age_in_community_review():
    html = (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "Review",
                "name": "Jaws 2",
                "datePublished": "1978-01-01",
                "audience": {"suggestedMinAge": 6},
            }
        )
        + "</script>"
    )
    assert parse_review(html, URL, item()) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",
        "//example.com/review",
        "https://www.commonsensemedia.org.evil.test/movie-reviews/jaws-2",
        "https://www.commonsensemedia.org/user/login",
        "https://www.commonsensemedia.org/tv-reviews/jaws-2",
        "https://www.commonsensemedia.org/movie-reviews/jaws-2?foo=bar",
    ],
)
def test_rejects_untrusted_or_wrong_type_review_urls(url):
    with pytest.raises(ValueError):
        valid_review_url(url, "movie")


@pytest.mark.parametrize(
    "age,kind,result",
    [
        (5, "movie", "G"),
        (10, "movie", "PG"),
        (11, "movie", "PG-13"),
        (13, "movie", "PG-13"),
        (14, "movie", "R"),
        (18, "movie", "NC-17"),
        (7, "series", "TV-Y7"),
        (13, "series", "TV-14"),
        (17, "episode", "TV-MA"),
    ],
)
def test_household_bands(age, kind, result):
    assert mapped_rating(age, kind) == result


def guidance(age=13):
    return Guidance(age, "Common Sense Media", URL, "exact title + release year", "Jaws 2", 1978)


def test_keeps_official_rating_and_unrelated_tags():
    i = item(official="PG", tags=["family", "Sitarr: Age 8+", "Sitarr: Unreviewed"])
    p = proposal(i, guidance())
    assert i.official == "PG"
    assert p["custom"] == "PG-13"
    assert "family" in p["tags"]
    assert "Sitarr: Age 13+" in p["tags"]
    assert "Sitarr: Age 8+" not in p["tags"]
    assert "Sitarr: Unreviewed" not in p["tags"]


@pytest.mark.parametrize(
    "official,custom,expected",
    [("R", None, "R"), ("PG", "R", "R"), ("PG", "Household adult", "Household adult")],
)
def test_never_weakens_existing_restriction(official, custom, expected):
    assert proposal(item(official=official, custom=custom), guidance(8))["custom"] == expected


def test_no_match_adds_review_tag_without_assigning_age():
    p = proposal(item(custom="PG-13", tags=["favorite"]), None)
    assert p == {"custom": "PG-13", "tags": ["Sitarr: Unreviewed", "favorite"]}


def test_apply_is_idempotent():
    p = proposal(item(official="PG"), guidance())
    assert proposal(item(official="PG", custom=p["custom"], tags=p["tags"]), guidance()) == p
