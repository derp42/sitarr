from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

from .config import Server
from .models import TAG_PREFIX, Item


class ServerError(Exception):
    pass


class MediaServer:
    def __init__(self, config: Server, client: httpx.Client | None = None):
        self.config = config
        headers = (
            {"X-Emby-Token": config.token} if config.kind == "jellyfin" else {"X-Plex-Token": config.token}
        )
        self.http = client or httpx.Client(base_url=config.url + "/", headers=headers, timeout=40)

    def close(self):
        self.http.close()

    def request(self, method, path, **kwargs):
        try:
            r = self.http.request(method, path.lstrip("/"), **kwargs)
            if not r.is_success:
                raise ServerError(f"{self.config.name} returned HTTP {r.status_code}")
            return r
        except httpx.RequestError:
            raise ServerError(f"Cannot reach {self.config.name}") from None


class Jellyfin(MediaServer):
    def metadata(self, remote_id):
        # Read through the administrator library API, without a viewer context.
        # Jellyfin 10.10's single-item endpoint needs a user, whose restrictions
        # can hide the item immediately after a successful rating change. That
        # endpoint therefore cannot verify/undo parental-control writes safely.
        fields = (
            "CustomRating,DateCreated,Genres,OriginalTitle,Overview,ParentId,Path,People,"
            "ProductionLocations,ProviderIds,Settings,SortName,SpecialEpisodeNumbers,"
            "Studios,Taglines,Tags,AirTime,RemoteTrailers,MediaSources,MediaStreams,"
            "DisplayPreferencesId,ExternalUrls"
        )
        result = self.request(
            "GET",
            "Items",
            params={
                "Ids": remote_id,
                "Fields": fields,
                "EnableUserData": "false",
            },
        ).json()
        item = next((d for d in result.get("Items", []) if d.get("Id") == remote_id), None)
        if not item:
            raise ServerError("Jellyfin item no longer exists in the administrator library")
        return item

    def from_json(self, d):
        kind = d.get("Type", "Movie").lower()
        return Item(
            self.config.id,
            d["Id"],
            d.get("Name", "Untitled"),
            d.get("ProductionYear"),
            "movie" if kind == "movie" else kind,
            d.get("OfficialRating"),
            d.get("CustomRating"),
            d.get("Tags") or [],
            {k.lower(): str(v) for k, v in (d.get("ProviderIds") or {}).items()},
            d.get("ParentId"),
            d.get("LockedFields") or [],
        )

    def all_items(self, parent=None):
        offset = 0
        while True:
            params = {
                "Recursive": "true",
                "IncludeItemTypes": "Season,Episode" if parent else "Movie,Series",
                "Fields": "ProviderIds,CustomRating,Tags,LockedFields,ParentId,Path",
                "StartIndex": offset,
                "Limit": 250,
                "EnableUserData": "false",
            }
            if parent:
                params["ParentId"] = parent
            response = self.request("GET", "Items", params=params).json()
            items = response.get("Items", [])
            for d in items:
                item = self.from_json(d)
                if not self.config.libraries or parent or d.get("ParentId") in self.config.libraries:
                    yield item
            offset += len(items)
            if not items or offset >= response.get("TotalRecordCount", offset):
                break

    def get(self, remote_id):
        return self.from_json(self.metadata(remote_id))

    def write(self, remote_id, state):
        # Jellyfin's update endpoint expects a complete metadata DTO. Fetch it
        # freshly; changing only our two fields preserves overview, IDs, images,
        # dates, genres, locks, and other edits made since Sitarr's library scan.
        metadata = self.metadata(remote_id)
        metadata["CustomRating"] = state["custom"]
        metadata["Tags"] = state["tags"]
        self.request("POST", f"Items/{remote_id}", json=metadata)
        actual = self.get(remote_id)
        if actual.custom != state["custom"] or sorted(actual.tags) != sorted(state["tags"]):
            raise ServerError("Jellyfin did not persist the requested rating and tags")
        return actual

    def users(self):
        return self.request("GET", "Users").json()

    def libraries(self):
        return self.request("GET", "Library/VirtualFolders").json()

    def policy(self, user_id):
        return self.request("GET", f"Users/{user_id}").json()["Policy"]

    def protect(self, user_id, age, block_unreviewed):
        policy = self.policy(user_id)
        # Keep unrelated tags and all existing permissions. Never alter admin
        # status, passwords, sharing, libraries or an existing stricter ceiling.
        blocked = [t for t in policy.get("BlockedTags", []) if not t.startswith(TAG_PREFIX)]
        blocked += [f"{TAG_PREFIX}Age {n}+" for n in range(age + 1, 22)]
        if block_unreviewed:
            blocked.append(TAG_PREFIX + "Unreviewed")
        policy["BlockedTags"] = sorted(set(blocked))
        current = policy.get("MaxParentalRating")
        policy["MaxParentalRating"] = min(age, current) if current is not None else age
        self.request("POST", f"Users/{user_id}/Policy", json=policy)
        return self.policy(user_id)

    def visible(self, user_id, ids):
        data = self.request(
            "GET",
            "Items",
            params={
                "UserId": user_id,
                "Ids": ",".join(ids),
                "Recursive": "true",
            },
        ).json()
        return [d["Id"] for d in data.get("Items", [])]


class Plex(MediaServer):
    """Plex uses contentRating and labels; there is no separate custom rating.

    We retain the original in Sitarr's ledger and write the stricter contentRating.
    Plex Home/sharing restrictions remain an explicit Plex administrator setting.
    """

    def tree(self, path, **kwargs):
        return ET.fromstring(self.request("GET", path, **kwargs).content)

    def from_xml(self, d):
        ids = {}
        for g in d.findall("Guid"):
            value = g.get("id", "")
            for provider in ["imdb", "tmdb", "tvdb"]:
                if value.startswith(provider + "://"):
                    ids[provider] = value.split("://", 1)[1].split("?")[0]
        kind = {"show": "series"}.get(d.get("type"), d.get("type", "movie"))
        custom = d.get("contentRating")
        return Item(
            self.config.id,
            d.get("ratingKey"),
            d.get("title", "Untitled"),
            int(d.get("year")) if d.get("year") else None,
            kind,
            custom,
            custom,
            [x.get("tag") for x in d.findall("Label")],
            ids,
            d.get("parentRatingKey"),
        )

    def libraries(self):
        return [
            {"Id": x.get("key"), "Name": x.get("title"), "Type": x.get("type")}
            for x in self.tree("library/sections").findall("Directory")
        ]

    def all_items(self, parent=None):
        if parent:
            for d in self.tree(f"library/metadata/{parent}/allLeaves", params={"includeGuids": 1}):
                if d.get("ratingKey"):
                    yield self.from_xml(d)
            return
        for section in self.libraries():
            if section["Type"] not in ("movie", "show"):
                continue
            if self.config.libraries and section["Id"] not in self.config.libraries:
                continue
            start = 0
            while True:
                tree = self.tree(
                    f"library/sections/{section['Id']}/all",
                    params={
                        "includeGuids": 1,
                        "X-Plex-Container-Start": start,
                        "X-Plex-Container-Size": 200,
                    },
                )
                entries = [d for d in tree if d.get("ratingKey")]
                for d in entries:
                    yield self.from_xml(d)
                start += len(entries)
                if not entries or start >= int(tree.get("totalSize", tree.get("size", str(start)))):
                    break

    def get(self, remote_id):
        tree = self.tree(f"library/metadata/{remote_id}", params={"includeGuids": 1})
        entry = next((d for d in tree if d.get("ratingKey")), None)
        if entry is None:
            raise ServerError("Plex item not found")
        return self.from_xml(entry)

    def write(self, remote_id, state):
        tree = self.tree(f"library/metadata/{remote_id}")
        d = next(iter(tree), None)
        if d is None or not re.fullmatch(r"\d+", str(remote_id)):
            raise ServerError("Plex item not found")
        section = d.get("librarySectionID") or tree.get("librarySectionID")
        if not section:
            raise ServerError("Plex did not return the library section")
        types = {"movie": 1, "show": 2, "season": 3, "episode": 4}
        params = {
            "type": types[d.get("type", "movie")],
            "id": remote_id,
            "contentRating.value": state["custom"] or "",
            "contentRating.locked": 1,
            "label.locked": 1,
        }
        existing = [x.get("tag") for x in d.findall("Label")]
        for index, tag in enumerate(sorted(set(state["tags"]) - set(existing))):
            params[f"label[{index}].tag.tag"] = tag
        removed = sorted(set(existing) - set(state["tags"]))
        if removed:
            params["label[].tag.tag-"] = ",".join(removed)
        self.request("PUT", f"library/sections/{section}/all", params=params)
        actual = self.get(remote_id)
        if actual.custom != state["custom"] or sorted(actual.tags) != sorted(state["tags"]):
            raise ServerError("Plex did not persist the requested rating and labels")
        return actual


def connect(server: Server, client=None):
    return (Jellyfin if server.kind == "jellyfin" else Plex)(server, client)
