from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass
class Item:
    server: str
    id: str
    title: str
    year: int | None
    kind: str
    official: str | None = None
    custom: str | None = None
    tags: list[str] = field(default_factory=list)
    ids: dict[str, str] = field(default_factory=dict)
    parent_id: str | None = None
    locked: list[str] = field(default_factory=list)

    @property
    def key(self):
        return f"{self.server}:{self.id}"

    def data(self):
        return asdict(self)


@dataclass
class Guidance:
    age: int
    source: str
    url: str
    matched_by: str
    title: str
    year: int | None
    details: dict = field(default_factory=dict)

    def data(self):
        return asdict(self)


# These are household US mappings, not new official certifications. Exact-age
# restrictions also use age tags, so e.g. an 8-year-old and a 10-year-old can differ.
MOVIE_LADDER = [(5, "G"), (10, "PG"), (13, "PG-13"), (17, "R"), (21, "NC-17")]
TV_LADDER = [(5, "TV-Y"), (7, "TV-Y7"), (10, "TV-PG"), (14, "TV-14"), (21, "TV-MA")]
SCORES = {
    "G": 0,
    "PG": 10,
    "PG-13": 13,
    "R": 17,
    "NC-17": 18,
    "TV-Y": 0,
    "TV-G": 0,
    "TV-Y7": 7,
    "TV-Y7-FV": 7,
    "TV-PG": 10,
    "TV-14": 14,
    "TV-MA": 17,
    "XXX": 21,
}
TAG_PREFIX = "Sitarr: "


def rating_score(value: str | None) -> int | None:
    if not value:
        return None
    return SCORES.get(value.upper().removeprefix("US-").strip())


def mapped_rating(age: int, kind: str) -> str:
    if not 0 <= age <= 21:
        raise ValueError("Age must be between 0 and 21")
    return next(rating for limit, rating in (MOVIE_LADDER if kind == "movie" else TV_LADDER) if age <= limit)


def proposal(item: Item, guidance: Guidance | None) -> dict:
    tags = [t for t in item.tags if not t.startswith(TAG_PREFIX)]
    if guidance is None:
        return {"custom": item.custom, "tags": sorted(set(tags + [TAG_PREFIX + "Unreviewed"]))}
    desired = mapped_rating(guidance.age, item.kind)
    # Never weaken an existing official or custom restriction. Preserve a
    # household override that cannot be interpreted rather than guessing.
    if item.custom and rating_score(item.custom) is None:
        desired = item.custom
    else:
        for rating in (item.official, item.custom):
            if rating_score(rating) is not None and rating_score(rating) > rating_score(desired):
                desired = rating
    return {
        "custom": desired,
        "tags": sorted(
            set(
                tags
                + [
                    f"{TAG_PREFIX}Age {guidance.age}+",
                    f"{TAG_PREFIX}Source {guidance.source}",
                ]
            )
        ),
    }


def managed_state(item: Item) -> dict:
    return {"custom": item.custom, "tags": sorted(item.tags)}


def age_from_tag(tags: list[str]) -> int | None:
    for tag in tags:
        if match := re.fullmatch(r"Sitarr: Age (\d+)\+", tag):
            return int(match[1])
    return None
