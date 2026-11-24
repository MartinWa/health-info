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
a private, host-local filesystem.

## Layout

```
health_info/   settings (base/development/production/test), server, urls, wsgi
records/       models, forms, views, registries, schedule (derived values), summary
records/db/    the WAL-enforcing SQLite backend
tests/         schedule, isolation, registry, UI, auth
tools/         container healthcheck
```
