from __future__ import annotations

import json
import re
import time
import unicodedata
from urllib.parse import quote, urlsplit

import httpx
from bs4 import BeautifulSoup

from .config import Settings
from .db import Store
from .models import Guidance, Item

CSM = "https://www.commonsensemedia.org"


class SourceUnavailable(Exception):
    """A transient or access failure, never a confirmed no-match."""


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9]", "", value)


def slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    value = value.replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def valid_review_url(url: str, kind: str) -> str:
    if url.startswith("/") and not url.startswith("//"):
        url = CSM + url
    p = urlsplit(url)
    path_type = "movie" if kind == "movie" else "tv"
    if (
        p.scheme != "https"
        or p.netloc != "www.commonsensemedia.org"
        or p.query
        or p.fragment
        or not re.fullmatch(rf"/{path_type}-reviews/[a-zA-Z0-9_-]+/?", p.path)
    ):
        raise ValueError("Use a Common Sense Media review URL for this media type")
    return url.rstrip("/")


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def parse_review(html: str, url: str, item: Item) -> Guidance | None:
    """Use editorial age metadata only, never parent/kid community age votes.

    Require exact identity. A similarly named sequel/remake is not evidence.
    Drupal's release date is the movie year; JSON-LD dateCreated is often the
    review publication date and must not be treated as a movie release year.
    """
    url = valid_review_url(url, item.kind)
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical:
        url = valid_review_url(canonical.get("href", url), item.kind)
    props = {}
    settings = soup.select_one('script[data-drupal-selector="drupal-settings-json"]')
    if settings:
        try:
            props = json.loads(settings.get_text()).get("csm", {}).get("amplitude_props", {})
        except (ValueError, AttributeError):
            pass
    editorial = None
    expected = "Movie" if item.kind == "movie" else "TVSeries"
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            for obj in objects(json.loads(script.get_text())):
                types = obj.get("@type", [])
                types = [types] if isinstance(types, str) else types
                if expected in types and obj.get("audience", {}).get("suggestedMinAge") is not None:
                    editorial = obj
                    break
        except (ValueError, AttributeError, TypeError):
            continue
    expected_review = "csm_movie" if item.kind == "movie" else "csm_tv"
    if props.get("csm_review_type") not in (None, expected_review):
        return None
    title = props.get("csm_review_target_title") or (editorial or {}).get("name")
    age = props.get("csm_review_rating_age")
    if age is None:
        age = (editorial or {}).get("audience", {}).get("suggestedMinAge")
    year = props.get("csm_title_release_date")
    if year is None:
        year = (editorial or {}).get("datePublished") or (editorial or {}).get("dateReleased")
    year_match = re.match(r"^(\d{4})(?:\D|$)", str(year or ""))
    year = int(year_match[1]) if year_match else None
    age_match = re.fullmatch(r"(?:age\s*)?(\d{1,2})\+?", str(age or ""), flags=re.I)
    if not title or not age_match or not 0 <= int(age_match[1]) <= 21:
        return None
    # The CSM public page generally has no IMDb id. Exact title AND release year
    # are mandatory in that case. Missing year is a review task, not permission.
    if normalized(title) != normalized(item.title) or not item.year or year != item.year:
        return None
    details = {}
    for name in ["violence", "sex", "language", "drugs"]:
        value = props.get("csm_review_rating_details_" + name)
        if isinstance(value, int):
            details[name] = value
    return Guidance(
        int(age_match[1]), "Common Sense Media", url, "exact title + release year", title, year, details
    )


class Sources:
    def __init__(self, store: Store, settings: Settings, client: httpx.Client | None = None):
        self.store = store
        self.settings = settings
        self.http = client or httpx.Client(
            timeout=20,
            follow_redirects=False,
            headers={
                "User-Agent": "Sitarr/0.1 (+https://github.com/derp42/sitarr; cached age-guidance lookup)",
            },
        )
        self.last_request = 0.0

    def close(self):
        self.http.close()

    def get(self, url: str):
        wait = self.settings.request_delay - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self.http.get(url)
            self.last_request = time.monotonic()
            if response.status_code in (301, 302, 307, 308):
                location = response.headers.get("location", "")
                # Don't forward requests to another host or to login/paywall URLs.
                location = valid_review_url(location, "movie" if "/movie-reviews/" in url else "series")
                response = self.http.get(location)
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise SourceUnavailable(
                    f"Common Sense Media returned HTTP {response.status_code}; lookup paused"
                )
            return response
        except httpx.RequestError:
            raise SourceUnavailable(
                "Common Sense Media is unreachable; existing ratings are preserved"
            ) from None

    def lookup(self, item: Item, explicit_url: str | None = None) -> Guidance | None:
        identity = item.ids.get("imdb") or item.ids.get("tmdb") or normalized(item.title)
        cache_key = f"csm:v1:{item.kind}:{identity}:{item.year}:{normalized(item.title)}"
        if explicit_url is None:
            hit, cached = self.store.cached(cache_key)
            if hit:
                return Guidance(**cached) if cached else None
        elif not self.settings.csm_enabled:
            raise ValueError("Public Common Sense Media lookups are disabled")
        if self.settings.mdblist_token and explicit_url is None:
            rating = self.mdblist(item)
            if rating:
                self.store.cache(cache_key, rating.data(), self.settings.cache_days)
                return rating
        if not self.settings.csm_enabled:
            return None
        path_type = "movie" if item.kind == "movie" else "tv"
        direct = (
            valid_review_url(explicit_url, item.kind)
            if explicit_url
            else f"{CSM}/{path_type}-reviews/{slug(item.title)}"
        )
        response = self.get(direct)
        if response:
            if result := parse_review(response.text, str(response.url), item):
                self.store.cache(cache_key, result.data(), self.settings.cache_days)
                return result
        if explicit_url:
            return None
        response = self.get(f"{CSM}/ajax/autocomplete/search/{quote(item.title, safe='')}")
        candidates = []
        if response:
            try:
                groups = response.json()
                for group in groups if isinstance(groups, list) else []:
                    for candidate in group.get("items", []):
                        if normalized(candidate.get("title", "")) == normalized(item.title):
                            try:
                                url = valid_review_url(candidate.get("url", ""), item.kind)
                                if url != direct and url not in candidates:
                                    candidates.append(url)
                            except ValueError:
                                continue
            except (ValueError, AttributeError):
                raise SourceUnavailable("Common Sense search response changed; lookup paused") from None
        for url in candidates[:5]:
            response = self.get(url)
            if response and (result := parse_review(response.text, str(response.url), item)):
                self.store.cache(cache_key, result.data(), self.settings.cache_days)
                return result
        self.store.cache(cache_key, None, min(self.settings.cache_days, 7))
        return None

    def mdblist(self, item: Item) -> Guidance | None:
        provider = "imdb" if item.ids.get("imdb") else "tmdb"
        remote_id = item.ids.get(provider)
        if not remote_id:
            return None
        if not re.fullmatch(r"tt\d+" if provider == "imdb" else r"\d+", remote_id):
            return None
        kind = "movie" if item.kind == "movie" else "show"
        try:
            response = self.http.get(
                f"https://api.mdblist.com/{provider}/{kind}/{remote_id}",
                params={"apikey": self.settings.mdblist_token},
            )
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise SourceUnavailable(f"MDBList returned HTTP {response.status_code}; check key or quota")
            data = response.json()
            ids = data.get("ids") or {}
            returned = ids.get(provider) or data.get(provider + "id")
            # Verify the ID in the response rather than trusting title similarity.
            if str(returned) != str(remote_id):
                raise SourceUnavailable("MDBList returned an unexpected title ID; no changes made")
            cs = data.get("commonsense")
            if cs in (None, False, 0, "0", "false", ""):
                return None
            age = data.get("age_rating")
            if age is None and isinstance(cs, int) and cs > 1:
                age = cs
            match = re.fullmatch(r"(\d{1,2})\+?", str(age or ""))
            if not match or not 0 <= int(match[1]) <= 21:
                return None
            return Guidance(
                int(match[1]),
                "Common Sense via MDBList",
                "https://mdblist.com/",
                f"exact {provider.upper()} ID",
                data.get("title", item.title),
                item.year,
            )
        except (httpx.RequestError, ValueError, AttributeError):
            raise SourceUnavailable("MDBList lookup failed; no changes made") from None
