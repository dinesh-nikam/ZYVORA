# PHONEINTEL Architecture Specification

## 1. System Components

### 1.1 Backend Core (`backend/app`)
- **FastAPI Framework**: High-performance asynchronous REST API.
- **SQLAlchemy 2.0 (asyncio)**: Object-relational mapping supporting both PostgreSQL (production) and SQLite (testing).
- **Alembic**: Database schema versioning with PostgreSQL-specific constraints (e.g. append-only triggers on `audit_logs`).
- **Cryptographic Security Layer (`app.core.crypto`)**:
  - `FieldCipher`: AES-256-GCM authenticated encryption supporting key rotation (`kid:key`).
  - `BlindIndexer`: HMAC-SHA256 blind indexing for privacy-preserving search.
  - `TokenService`: JWT authentication with HS256 / RS256 token issuance and revocation tracking.
- **SSRF Guarded HTTP Client (`app.integrations.http`)**: Connect-time IP inspection preventing SSRF attacks against private networks and cloud metadata servers.
- **Verification Engine (`app.services.verification`)**: 10-stage verification pipeline for candidate URLs.
- **Confidence Scoring (`app.services.verification.confidence`)**: Multi-factor scoring model capped strictly without verified phone references.
- **Background Worker (`app.workers`)**: Redis-backed Arq queue handling discovery tasks with exponential backoff and dead-letter queue (DLQ).

### 1.2 Command-Line Interface (`cli/phoneintel_cli`)
- Built with **Typer** and **Rich**.
- Cross-platform support (Linux, macOS, Windows, Termux).
- Support for human-readable tables/panels and machine-readable JSON output (`--json`).

### 1.3 Telegram Bot (`telegram_bot/phoneintel_bot`)
- Built with **Aiogram 3**.
- Service account delegation (`telegram:act_as` permission) ensuring strict user isolation.
- Automatic message chunking (4000-character limits) and PDF report delivery.

---

## 2. Data Flow: Profile Discovery & Verification

```
[User Request]
       │
       ▼
[Investigation Created] ──► Validate E.164, Encrypt Purpose, Record Audit
       │
       ▼
[Job Enqueued] ──────────► Redis Queue (Arq) / Inline Worker
       │
       ▼
[Discovery Phase] ───────► Query Search Engines & Platform APIs
       │
       ▼
[Verification Phase] ────► 10-Stage Evaluation (Syntax, Domain, Availability, Metadata)
       │
       ▼
[Confidence Scoring] ────► Apply Factor Points & Capping Rules
       │
       ▼
[Persistence] ───────────► Discovered Profiles & Evidence Sources
       │
       ▼
[Events / WebSockets] ───► Publish Progress Updates & Final Completion
```
