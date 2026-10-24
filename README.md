# Sitarr

**The library sitter.** Age guidance and household parental controls for Jellyfin and Plex.

An old film's official PG certificate can be a poor guide to what your family wants to watch
today. Sitarr adds a second opinion without pretending to issue new official certifications.
For example, Common Sense Media recommends **Jaws 2 (1978) for ages 13+**; Sitarr can preserve
its official PG certificate in Jellyfin, add an age-13 tag, and apply a household PG-13 custom
rating. A viewer limited to PG will no longer see it.

## What it does

- Scans movies and TV series in Jellyfin or Plex over their HTTP APIs.
- Fetches editorial age recommendations from public Common Sense Media review metadata,
  or optionally through MDBList using a personal API key.
- Requires **exact title and original release year** for public reviews, or a verified IMDb/TMDb
  ID for MDBList. Remakes, sequels, missing years, and uncertain matches go to review.
- Offers a searchable library, source links, a recommendation preview, and household overrides.
- Preserves stricter existing restrictions and unrelated metadata, tags, and account permissions.
- Writes series guidance to their episodes too; Jellyfin seasons are included.
- Records every remote write before sending it, verifies persistence, and supports undo with
  conflict detection. Later manual edits are never silently overwritten by an undo.
- Adds exact-age tag restrictions to selected Jellyfin viewers while keeping existing ceilings.
- Runs scheduled lookups with persistent caching, bounded batches, and courteous request pacing.

Jellyfin keeps `OfficialRating` unchanged and receives `CustomRating` plus `Sitarr: Age 13+`
and source tags. **Plex has no equivalent custom rating field**: Sitarr changes its content
rating and labels, locks those Plex fields, and keeps the original values in Sitarr's history.
Plex Home/sharing restrictions must be configured in Plex; Sitarr does not change Plex accounts.

## Run with Docker Compose

```sh
git clone https://github.com/derp42/sitarr.git
cd sitarr
mkdir -p data
SITARR_UID="$(id -u)" SITARR_GID="$(id -g)" docker compose up -d --build
```

Open **http://localhost:7171**. Sign in as **admin** using the password generated in
`./data/admin-password`:

```sh
cat data/admin-password
```

Connect a server under **Connections** using its URL and an administrator API key (Jellyfin)
or server token (Plex). Test the connection, then **Scan library** and **Check ages**.
The default is **review first**: inspect the recommendations and click **Apply reviewed**.
Enable automatic apply under **Guidance & rules** when the mappings fit your household.

The port is bound to localhost by default. Set `SITARR_BIND_ADDRESS` to your LAN interface
if other devices need access. Use a TLS reverse proxy for access beyond a trusted LAN.
Credentials and source keys live in the data directory and are never returned by the settings API.

### Install without Docker

Python 3.11 or newer is required:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
SITARR_DATA=/path/to/private/data .venv/bin/uvicorn sitarr.app:app --host 127.0.0.1 --port 7171
```

A hardened systemd unit is included at `deploy/sitarr.service`. It expects the application in
`/opt/sitarr`, a virtual environment in `/opt/sitarr/.venv`, and a dedicated `sitarr` system user.
It writes only to `/var/lib/sitarr`; it does not require a media-folder mount or write access to
your video files. The entire integration uses supported server APIs.

## Parental-control behavior

Sitarr's default US household mappings are deliberately explicit:

| Recommended age | Movie custom rating |
| --- | --- |
| 0–5 | G |
| 6–10 | PG |
| 11–13 | PG-13 |
| 14–17 | R |
| 18–21 | NC-17 |

TV bands are TV-Y (0–5), TV-Y7 (6–7), TV-PG (8–10), TV-14 (11–14), and TV-MA (15+).
These bands do not claim equivalence to official ratings-board decisions. They round upward
and never weaken a recognized existing official/custom restriction. An unrecognized household
custom rating is preserved instead of guessed at.

Under **Connections → Protected viewers**, choose a Jellyfin user and maximum recommended age.
Sitarr adds blocked tags for ages above that limit, preserving existing unrelated tags and
permissions. Existing lower rating ceilings remain in force; **the age recommendation is an
additional restriction, not a promise to grant access**. For example, TV-14 can still be blocked
by a user's existing PG-13 ceiling even if its age recommendation is 13.

The optional **block unreviewed** setting blocks titles bearing `Sitarr: Unreviewed`. It only
affects titles Sitarr has checked and tagged; it does not magically cover unscanned titles.
By default, unreviewed titles remain subject to the server's original restrictions. Titles with
no provider match need household review. No age source covers everything, and a series-level
recommendation cannot describe every episode. Test with an actual restricted account/client.

Administrator accounts retain the ability to change their own restrictions. Sitarr never
demotes an administrator or changes passwords. Existing TV sessions do not need to be signed out.

## Sources and data handling

Public review lookups use only publicly accessible editorial metadata. Requests are paced
(at least one second apart, two by default), positive results are cached for 30 days, and
confirmed misses for up to seven days. HTTP access failures and quotas pause a run rather than
being mistaken for missing ratings. Sitarr does not bypass authentication or paywalls, archive
review text, use AI to invent ages, or redistribute a scraped ratings database.

An optional MDBList API key enables ID-based Common Sense lookups. Availability and quotas
depend on the provider. The service remains useful without a key through public review lookups
and household review. Provider markup can change; uncertain matches are left untouched.

## Operations

- `/health` is an unauthenticated liveness endpoint containing only service version/status.
- The UI and `/api/*` require HTTP Basic authentication. `SITARR_USERNAME` defaults to `admin`.
  Set `SITARR_PASSWORD` to override the generated password.
- Browser writes reject cross-origin requests. Server tokens are sent in headers, not logged URLs.
- **Activity & history** shows jobs, errors, applied changes, and individual undo actions.
  Export history via the UI or authenticated `GET /api/export`.
- Back up the entire data directory, especially `sitarr.db`, `settings.json`, and
  `admin-password`. Keep it outside your public source repository.
- An interrupted remote write is marked `uncertain` in history. Inspect remote state before
  retrying. Undo only proceeds when the current fields still equal Sitarr's recorded write.
- Reverting a change pauses automatic lookup for its title. You can explicitly check it again
  after review. Each series/season/episode write has an individual ledger entry.
- Scheduled runs scan libraries and check ages in bounded batches. Initial backfill continues
  automatically at provider pacing and yields to interactive jobs. New episodes receive known
  series guidance on later scans.
- Run one application worker per data directory. Multiple Uvicorn workers are not supported.

## Development

```sh
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
.venv/bin/ruff check sitarr tests
```

Tests cover identity matching, outages, age mappings, permission preservation, Jellyfin metadata
writes, mocked Plex reads/writes, audit/undo conflicts, authentication, masked keys, and CSRF.
Plex integration has protocol-level tests; a live Plex server is needed for deployment acceptance.

## Inspiration

The implementation is original Python code. These projects informed the approach:

- [CS Ratings for Jellyfin](https://github.com/coder2h2/CS-Ratings-for-Jellyfin): editorial ages,
  review discovery, and source attribution.
- [Ratings Fixer](https://github.com/dissentingd/jellyfin-ratings-fixer): stricter-only changes,
  review workflows, series propagation, and an audit ledger.
- [Plex Common Sense Collections](https://github.com/zwgtdev/Plex-Common-Sense-Collections):
  cached public review lookup and bounded processing.
- [Plex Family Management](https://github.com/Mike7154/plex_family_management): household age labels.

Sitarr is not affiliated with or endorsed by Jellyfin, Plex, Common Sense Media, MDBList, Sonarr,
or Radarr. Review information belongs to its source. See [LICENSE](LICENSE) for the code license.

