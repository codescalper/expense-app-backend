# Family Budget

A self-hostable family expense and budget manager: a FastAPI + PostgreSQL backend and an Expo
(React Native) mobile app. Built for 5–10 family members with private and shared finances, and
fast UPI expense logging for India.

**Repositories:** this one holds the backend, infrastructure and docs
([codescalper/expense-app-backend](https://github.com/codescalper/expense-app-backend)); the Expo
mobile app is [codescalper/expense-app](https://github.com/codescalper/expense-app). For local
development, clone the mobile repo into `./mobile`:

```bash
git clone https://github.com/codescalper/expense-app-backend.git expense
cd expense && git clone https://github.com/codescalper/expense-app.git mobile
```

> **Status:** all product phases are built and tested: auth, families and roles, accounts,
> categories, transactions, UPI payment logging, budgets, recurring bills, savings goals,
> notifications/push, reports and the dashboard. See [PROGRESS.md](PROGRESS.md) for verified
> results and known limitations.

## Stack

| Layer | Tech |
|---|---|
| Mobile | Expo SDK 58, Expo Router, TypeScript (strict), TanStack Query, Zustand, React Hook Form, Zod |
| API | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async) + asyncpg, Alembic |
| Data | PostgreSQL only: local via Docker, Neon in staging/production |
| Background work | Worker process using a PostgreSQL job queue (`FOR UPDATE SKIP LOCKED`), no Redis |
| Dev email | Mailpit (catches all outgoing mail locally) |

## Quick start (local)

Requirements: Docker Desktop, Node 20+ (for the mobile app).

```bash
cp .env.example .env
docker compose up -d --build        # postgres, mailpit, api (runs migrations), worker
curl http://localhost:8000/health/ready
```

- API docs (OpenAPI): http://localhost:8000/docs
- Dev inbox (verification / reset codes): http://localhost:8025

Mobile app:

```bash
cd mobile
npm install
cp .env.example .env                # set EXPO_PUBLIC_API_URL, see mobile/README.md
npx expo start
```

Sign-up flow: register in the app, open the 6-digit code from Mailpit, enter it, and you're in.

## Tests and checks

```bash
# backend (inside the api container: same Python and deps as CI)
docker compose run --rm --no-deps api pytest -q
docker compose run --rm --no-deps api sh -c "ruff check . && ruff format --check . && mypy app"

# mobile
cd mobile && npm run typecheck && npm run lint && npm test
```

CI (`.github/workflows/ci.yml`) runs all of the above, plus migrations, `pip-audit`, `npm audit`,
an Android bundle build and the production Docker image build.

## Repository layout

```
backend/        FastAPI app: api/ → services/ → repositories/ → models/; alembic/; tests/
mobile/         Expo app: src/app (screens), src/api, src/store, src/components, src/theme
docs/           architecture, ER diagram, environment, Neon, migrations, backup, security, testing, deployment
infra/          local Postgres init script
docker-compose.yml
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) · [Database ER diagram](docs/ER.md)
- [Environment variables](docs/ENVIRONMENT.md) · [Neon setup](docs/NEON.md)
- [Migrations](docs/MIGRATIONS.md) · [Backup & restore](docs/BACKUP_RESTORE.md)
- [Security](docs/SECURITY.md) · [Testing](docs/TESTING.md) · [Deployment](docs/DEPLOYMENT.md)

## Security notes

Never commit `.env` files. The app never asks for or stores UPI PINs, card CVVs, or bank
passwords. Report security issues privately to the maintainers rather than in public issues.
