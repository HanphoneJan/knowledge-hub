# Knowledge Hub · Multi-source Knowledge Base with RAG

[![ci](https://github.com/HanphoneJan/knowledge-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/HanphoneJan/knowledge-hub/actions/workflows/ci.yml)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.142-009688.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL+pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1.svg)](https://github.com/pgvector/pgvector)
[![Qdrant](https://img.shields.io/badge/Qdrant-1.15-DC244C.svg)](https://qdrant.tech/)
[![React 19](https://img.shields.io/badge/React-19-61DAFB.svg)](https://react.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

**A self-hosted, multi-tenant knowledge base with citation-grounded RAG.**
Upload documents / photos → parse → chunk → embed → **four retrieval legs** (dense + BM25 + trigram + visual)
→ ask questions and get answers where **every claim links back to the exact page region it came from**.
Ships an **MCP** server (for AI clients) and an **A2A** agent (for other agents) on top of the same retrieval core.

- **Verifiable** — citations are L3-grade: the answer's `[n]` markers open a drawer with the page image and the matched box.
- **No data egress by default** — the default generator is *extractive* (answers are verbatim source sentences);
  an external LLM is opt-in and configured explicitly.
- **Operable** — job queue with per-document progress, index reconciliation, backup + **restore drill**
  (table row counts and per-blob `sha256` verified).
- **Structured tables** — besides Markdown text, every table gets a JSON grid (`chunks.table_json`,
  rendered as a real table in the UI) and a rule-based caption that is indexed for retrieval
  (no external LLM involved, zero egress).

| | |
|---|---|
| API contract | **82** endpoints declared / 81 implemented / 1 deliberately not implemented — checked by `eval/contract_check.py` (paths, methods, **query-param surface**) |
| Tests | pytest **1335** · frontend vitest **316** · three acceptance suites (138 / 218 / 199) |
| Retrieval | RRF fusion of dense (bge-small-zh) · self-implemented BM25 · trigram · **visual** (colSmol late-interaction multi-vector MaxSim) |
| Frontend | React 19 + Vite 7 + Tailwind v4 · **no CDN, no web fonts** (works fully offline on a LAN) |

Screenshots use **synthetic demo documents** — see `docs/img/` and the Chinese README for the full tour.

## Quick start

```bash
bash deploy/dev.sh start      # API :8010 + worker + visual service :8020
bash deploy/dev.sh restart    # after changing backend code
bash deploy/visual.sh {start|stop|restart|status|logs}   # manage the 2.65 GB visual service alone
cd web && npm run build       # frontend build is served by the API at http://127.0.0.1:8010
```

Full runbook: [`docs/README.md`](docs/README.md) · [`docs/architecture.md`](docs/architecture.md) ·
[`docs/retrieval.md`](docs/retrieval.md) · [`docs/ingestion.md`](docs/ingestion.md) ·
[`docs/DESIGN-API.md`](docs/DESIGN-API.md) · [`web/README.md`](web/README.md) · [`deploy/README.md`](deploy/README.md).

## CI vs. full baseline

Hosted CI runs the **infrastructure-free gate** (`bash deploy/ci.sh`): frontend typecheck + unit tests + build,
API contract coverage, and 443 backend tests that need neither a database, a running server, nor the embedding models —
the full 1335-test baseline additionally needs PostgreSQL(pgvector), Qdrant and locally cached models
(model loading is offline by design), so it is run locally.

## License

Project code is **MIT** ([`LICENSE`](./LICENSE)). Six UI primitives and the design tokens are copied from
[RAGFlow](https://github.com/infiniflow/ragflow) (**Apache-2.0**) — see
[`web/THIRD-PARTY-NOTICES.md`](./web/THIRD-PARTY-NOTICES.md); the license text is bundled at
[`web/THIRD-PARTY-LICENSES/Apache-2.0.txt`](./web/THIRD-PARTY-LICENSES/Apache-2.0.txt).
The chat layout is modelled on [Open WebUI](https://github.com/open-webui/open-webui) (design only — no code copied).
