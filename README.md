# health-info

One self-hosted service for one household's authored health records: a server-rendered UI,
one SQLite file, one container, no build step. Device/series aggregation belongs to Open
Wearables; this service joins by date. The full plan is [planning/PLAN.md](planning/PLAN.md).

## What exists in this build

Slice 1–4 of the plan's §11 build order:

- Accounts and context: person, environment, goals, programme, dashboard, domain state,
  audit events, per-account timezone.
- Medical context: conditions, injuries, allergies, medication courses with a schedule
  builder, practitioners, vaccinations, appointments.
- Logs: measurements (blood pressure as a panel plus components), symptoms, check-ins, dose
  events, periods, notes, timeline, derived expected doses and adherence.
- Memory: entries, corrections with the supersede chain, pin/lock, close follow-up,
  `/memory/review` for client-written rows, promoting a symptom into an injury or condition,
  API tokens, settings and audit.

The JSON API (§5), labs, import, backup/restore and export are not built yet. Where the plan
says "API", the UI writes today's records only.

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
in the proxy. `/healthz` is the only anonymous route. `/data` holds `db.sqlite3` and must be
a private, host-local filesystem. `compose.host-port.yaml` publishes `127.0.0.1:8000` for a
host proxy; `compose.local-build.yaml` builds the image from this working copy instead of
pulling a published one.

## Development container

`compose.development.yaml` is a standalone development stack: it builds the `development`
image target, needs no `.env`, serves on `127.0.0.1:8000` only, and keeps all state —
database, virtualenv, caches — on named volumes while the working copy is bound into the
container. Rebuild after a dependency change so the seeded virtualenv is refreshed.

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
down-migrations; the operations plan in [planning/PLAN.md](planning/PLAN.md) §9 says how to
back up and restore.

## License

PolyForm Noncommercial License 1.0.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE));
commercial use needs a separate license. [CONTRIBUTING.md](CONTRIBUTING.md) covers the
rules and the contributor agreement.

## Layout

```
health_info/   settings (base/development/production/test), server, urls, wsgi
records/       models, forms, views, registries, schedule (derived values), summary
records/db/    the WAL-enforcing SQLite backend
tests/         schedule, isolation, registry, memory, UI, auth
tools/         healthcheck, production check, image audit, release and hygiene scripts
```
