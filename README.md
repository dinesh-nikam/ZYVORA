# PHONEINTEL

**PHONEINTEL** is an enterprise-grade phone-number intelligence, numbering-plan verification, and **public** social-profile discovery platform. It is engineered with strict ethical constraints, defensive security controls, and transparent confidence scoring.

---

## Key Principles & Compliance

1. **Public-Source Only**: Discovers profiles solely through permitted public search engines, official platform APIs (YouTube, GitHub, Reddit, Telegram), investigator-supplied links, and robots.txt-compliant public pages.
2. **Zero Abuse / No Account Enumeration**: Never attempts address-book syncing, brute-force enumeration, password guessing, or social media anti-bot/privacy bypass.
3. **Evidence-Based Non-Attribution**: Explicitly distinguishes between:
   - **(A)** Public profile URL exists.
   - **(B)** Public evidence explicitly references the phone number.
   - **(C)** Profile ownership independently verified.
   *(A is never misrepresented as proof of B or C).*
4. **Data Privacy & Cryptographic Protection**: Phone numbers, purposes, and case references are AES-GCM encrypted at rest with blind HMAC indexing for exact lookups. Append-only audit logs track all access.
5. **Multi-Interface Access**: Backend REST API (FastAPI), Terminal CLI (`phoneintel`), and Telegram Bot (`phoneintel-bot`).

---

## Architecture Overview

```
                         ┌─────────────────────┐
                         │   Terminal CLI      │
                         │    (phoneintel)     │
                         └──────────┬──────────┘
                                    │ HTTP
┌─────────────────────┐             │             ┌─────────────────────┐
│    Telegram Bot     ├─────────────┼────────────►│     FastAPI API     │
│   (phoneintel-bot)  │  (Delegate) │             │    (/api/v1/...)    │
└─────────────────────┘             │             └──────────┬──────────┘
                                    │                        │
                                    ▼                        ▼
                         ┌──────────────────────────────────────────────┐
                         │              PostgreSQL 16                   │
                         │  (Encrypted Fields, Audit Logs, Migrations)  │
                         └──────────────────────────────────────────────┘
                                    ▲                        ▲
                                    │                        │
                         ┌──────────┴──────────┐  ┌──────────┴──────────┐
                         │     Redis 7 / ARQ   │  │   Safe HTTP Client  │
                         │  (Job Queue, State) │  │  (Connect-time SSRF)│
                         └─────────────────────┘  └─────────────────────┘
```

---

## Quickstart

### Prerequisites
- Python 3.12+
- PostgreSQL 16+ (or SQLite in local dev/testing)
- Redis 7+ (or in-memory mock in testing)
- `uv` (recommended) or `pip`

### 1. Installation

Clone and install dependencies into a virtual environment:
```bash
# Create and activate virtual environment
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install deployable packages in editable mode
uv pip install -e backend -e cli -e telegram_bot
```

### 2. Environment Configuration

Copy the example configuration:
```bash
cp backend/.env.example .env
```
Key configuration parameters:
- `DATABASE_URL`: `postgresql+asyncpg://phoneintel:phoneintel@localhost:5432/phoneintel`
- `REDIS_URL`: `redis://localhost:6379/0`
- `SECRET_KEY`: Minimum 32-character high-entropy secret key
- `BLIND_INDEX_KEY`: Minimum 32-character secret key for blind hashing
- `FIELD_ENCRYPTION_KEYS`: `k1:<base64-encoded-32-byte-key>`

### 3. Database Migration

Run database migrations to initialize tables and seed default roles (`admin`, `analyst`, `auditor`, `service_telegram`):
```bash
cd backend
alembic upgrade head
cd ..
```

### 4. Running Services

**Start API:**
```bash
uvicorn app.main:app_factory --factory --reload --port 8000
```

**Start Background Job Worker:**
```bash
arq app.workers.worker.WorkerSettings
```

**Start Telegram Bot:**
```bash
phoneintel-bot
```

---

## Terminal CLI (`phoneintel`)

The command-line interface provides full investigation, validation, and reporting capabilities:

```bash
# Show all commands
phoneintel --help

# Validate phone number format (E.164)
phoneintel validate +919876543210
phoneintel validate +12025550123 --json

# Country and regional plan lookup
phoneintel country +919876543210

# Carrier and operator lookup
phoneintel carrier +919876543210

# Discover public profiles
phoneintel profile-links +919876543210 --purpose "Fraud triage ticket 4411"

# Verify an individual URL with 10-stage pipeline
phoneintel verify-url https://t.me/example_user --phone +919876543210

# View past investigations
phoneintel history

# Export investigation report (JSON / PDF / CSV / HTML)
phoneintel report <INVESTIGATION_ID> --format pdf --output report.pdf

# Permanently erase investigation data
phoneintel delete-data <INVESTIGATION_ID>
```

---

## Telegram Bot (`phoneintel-bot`)

The bot operates as an aiogram-powered thin client delegating to the backend API via service account tokens.

### Available Commands
- `/start` — Welcome and onboarding guide
- `/help` — Complete command reference
- `/link <CODE>` — Link Telegram account to a PHONEINTEL user account
- `/unlink` — Disconnect linked account
- `/validate <phone>` — Check phone validity and standard format
- `/country <phone>` — Country and region details
- `/carrier <phone>` — Carrier metadata
- `/profile-links <phone> [purpose]` — Execute public profile discovery
- `/search-public <phone>` — Search public web sources
- `/report <id>` — Summary and PDF report document delivery
- `/history` — List recent user investigations (strictly user-isolated)
- `/delete-data <id>` — Permanently erase investigation records
- `/privacy` — Privacy and transparency terms

---

## Testing & Quality Assurance

PHONEINTEL maintains strict code quality, type safety, and test coverage:

```bash
# Run complete test suite (259 tests across backend, CLI, and Telegram bot)
pytest -v

# Run linter
ruff check .

# Run static type checker
mypy backend/app
```

---

## Security Architecture

- **Connect-time SSRF Guard**: Outbound requests perform DNS resolution and check every resolved IP against private, loopback, multicast, link-local, carrier-grade NAT, and cloud metadata ranges (`169.254.169.254`) before opening a socket.
- **Envelope Encryption**: Sensitive investigation fields are encrypted using AES-256-GCM.
- **Blind HMAC Indexing**: Exact lookups are performed on cryptographically hashed values, preventing plaintext leaks in database indices.
- **Append-Only Audit Logs**: Database triggers in PostgreSQL prevent modification of audit records.
- **Rate Limiting**: Granular token-bucket rate limits on authentication, phone lookups, discovery jobs, and verification requests.
- **Strict User Isolation**: All multi-tenant queries are scoped to the authenticated principal ID.
