# healthinfo

One self-hosted service for one household's authored health records: a server-rendered UI,
one SQLite file, one container, no build step. Device/series aggregation belongs to Open
Wearables; this service joins by date. The full plan is [planning/PLAN.md](planning/PLAN.md).

## What exists in this build

All six slices of the plan's §10 build order:

- Accounts and context: person, environment, goals, programme, dashboard, domain state,
  audit events, per-account timezone.
- Medical context: conditions, injuries, allergies, medication courses with a schedule
  builder, practitioners, vaccinations, appointments.
- Logs: measurements (blood pressure as a panel plus components), symptoms, check-ins,
  doses, events, notes, timeline, derived expected doses and adherence.
- Memory: entries, corrections with the supersede chain, pin/lock, close follow-up,
  `/memory/review` for client-written rows, promoting a symptom into an injury or condition,
  API tokens, settings and audit.
- JSON API at `/api/v1`: bearer tokens from `/tokens`, one success envelope, page blocks,
  owner-scoped ids (`404`, never `403`), the write rules of §5, `/summary`,
  `/expected-doses`, `/adherence`, `/health`, plus public `/docs` (Swagger) and
  `/openapi.json`. The schema is generated from the routes and served at
  `/api/v1/openapi.json` and `/api/v1/docs`.
- Labs: `/labs` lists panels, `/labs/new` adds one panel's analytes in a single form and
  pre-fills analytes, units and reference bounds from the last panel with the same name.
- Polish: `/export` (zip of the account's JSON), `/purge`, `/search` and `/llms.txt`.

### Backup and restore

`compose.backup.yaml` mounts `${APP_BACKUP_PATH}` at `/backups`; the nightly run is one
verified copy, and older ones are pruned:

```sh
docker compose -f compose.yaml -f compose.backup.yaml run --rm app \
  python manage.py backup_health_info /backups/2026-11-24T0310
```

`restore_health_info <backup> <destination>` re-checks a backup and writes
`<destination>/db.sqlite3`; it never edits a live file. `<destination>` is a host path under
`${APP_RESTORE_PATH}`, which `compose.restore.yaml` mounts at `/restores`. First write the
copy with a one-shot container — `/backups` comes from `compose.backup.yaml`, not from the
restore one:

```sh
docker compose -f compose.yaml -f compose.backup.yaml -f compose.restore.yaml run --rm app \
  python manage.py restore_health_info /backups/2026-11-24T0310 /restores/check-1
```

Then boot that copy on the spare port under its own project, so the drill gets its own
container and can neither recreate nor detach the live app:

```sh
APP_DATA_PATH="$APP_RESTORE_PATH/check-1" docker compose -p health-info-restore \
  -f compose.yaml -f compose.restore.yaml up -d
```

Stop the drill first — `docker compose -p health-info-restore down` — so that only one
container ever has `/data`. Point `APP_DATA_PATH` at that directory only once it has answered
on `127.0.0.1:8001`.

## Run it locally

```sh
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

Development data lives in `.data/` and is not tracked.

## Check it

```sh
uv run ruff check . && uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
uv run pytest -q
```

## Run it as a container

```sh
cp .env.example .env      # fill in, chmod 600
docker compose -f compose.yaml -f compose.proxy-network.yaml up -d
```

The app listens on `0.0.0.0:8000` inside the container and never publicly; TLS terminates
in the proxy. `/healthz`, `/login`, `/logout`, `/password`, `/favicon.ico` and the API's own
`/api/v1/docs` and `/api/v1/openapi.json` are anonymous; every other route needs a session or
a token. `/data` holds `db.sqlite3` and must be a private, host-local filesystem.
`compose.host-port.yaml` publishes `127.0.0.1:8000` for a host proxy;
`compose.local-build.yaml` builds the image from this working copy instead of pulling a
published one.

## Development container

`compose.development.yaml` is a standalone development stack: it builds the `development`
image target, needs no `.env`, serves on `127.0.0.1:8000` only, and keeps all state —
database, virtualenv, caches — on named volumes while the working copy is bound into the
container. Rebuild after a dependency change so the seeded virtualenv is refreshed.
`HEALTH_INFO_DEV_PORT` moves the published host port, for a host that already uses 8000:

```sh
HEALTH_INFO_DEV_PORT=8020 docker compose -f compose.development.yaml up --build
```

The first account is `docker compose -f compose.development.yaml exec web python manage.py
createsuperuser`.

```sh
docker compose -f compose.development.yaml up --build
docker compose -f compose.development.yaml run --rm web ruff check .
docker compose -f compose.development.yaml run --rm web pytest -q
docker compose -f compose.development.yaml down
```

CI runs every check in this container, plus the lockfile check, the dependency and image
audits and the production-image check.

## Releases

Only `main` is protected; changes land through a pull request whose CI is green. A release
is a stable `X.Y.Z` tag on a tested `main` commit:

```sh
pwsh -File tools/tag_release.ps1 -Push
```

The tag triggers the release workflow, which builds `linux/amd64` and `linux/arm64`, runs
`tools/check_production.py` against each image, audits them, publishes the multi-platform
image to `ghcr.io/martinwa/health-info`, and creates the GitHub Release. There are no
down-migrations; the operations plan in [planning/PLAN.md](planning/PLAN.md) §8 says how to
back up and restore.

## License

PolyForm Noncommercial License 1.0.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE));
commercial use needs a separate license. [CONTRIBUTING.md](CONTRIBUTING.md) covers the
rules and the contributor agreement.

## Layout

```
health_info/   settings (base/development/production/test), server, urls, wsgi
records/       models, forms, views, api, registries, schedule (derived), summary
records/db/    the WAL-enforcing SQLite backend
tests/         schedule, isolation (UI and API), registry, memory, UI, auth
tools/         healthcheck, production check, image audit, release scripts
```
