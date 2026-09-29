# AI-Powered Documentation–Script Consistency Analyzer (Linux on IBM Z)

Compares the **build documentation** of [`linux-on-ibm-z/docs`](https://github.com/linux-on-ibm-z/docs/wiki)
with the **automated build scripts** of [`linux-on-ibm-z/scripts`](https://github.com/linux-on-ibm-z/scripts),
and reports every place where they disagree — missing prerequisites, environment
variables, build flags, and undocumented s390x patches.

```
wiki page  Building-NGINX.md ─┐
                              ├─► RAG (ChromaDB/Qdrant) ─► rules + LLM ─► discrepancy report
script     NGINX/1.30.4/build_nginx.sh ─┘
```

## Why the matching is deterministic first

Retrieval is **not** "find the most similar document". Documents and scripts are
paired on an explicit key:

```
match_key = f"{normalize_package_name(name)}::{distro}"   # e.g. "apachehttpserver::rhel"
```

* `Building-<Package>.md` → `docs/wiki/Building-NGINX.md`, `scripts/<Package>/<Version>/build_<Package>.sh`
* names are lower-cased with separators stripped, so `Building-Apache-HTTP-Server.md`
  pairs with `scripts/ApacheHttpServer/*` and `Building-NGINX.md` with `scripts/NGINX/*`
* in `linux-on-ibm-z/scripts` the **directory** is the package identity and the
  file name is only the variant (`ApacheKafka/2.8.0/build_kafka_AdoptiumTemurin.sh`)
* leftover names fall back to ordered, reported rules: `version-stripped`
  (`Building-ANTLR-4` → `AntLR`), `version-variant` (`Building-Apache-Cassandra-3.11`
  shares the `ApacheCassandra` tree), `suffix`, then `fuzzy`

Every chunk carries `package_name`, `normalized_name`, `distro`, `source_type`,
`match_key`, `source_path`, `commit` and `chunk_index`, so analysis-time retrieval
is a metadata filter over an already-matched pair and semantic search only ranks
*within* it. The LLM can therefore never compare Nginx against the PostgreSQL script.

## Directory structure

```
.
├── app/
│   ├── main.py           FastAPI app: /api/sync, /api/packages, /api/analyze, /api/results/{pkg}, /health
│   ├── config.py         env-driven settings (pydantic-settings)
│   ├── models.py         Pydantic schemas: extraction components, LLM contract, HTTP DTOs
│   ├── parsers.py        deterministic extraction from markdown + shell (+ chunking, name matching)
│   ├── ingestion.py      git sync, discovery/pairing, chunk building, embedding, indexing, pruning
│   ├── analyzer.py       retrieval, rule engine, prompt, LLM merge, scoring, persistence
│   ├── llm.py            Instructor structured-output client (openai | anthropic | mock)
│   ├── vector_store.py   ChromaDB / Qdrant adapters + embeddings + filter DSL
│   ├── db.py             SQLAlchemy 2.0 persistence (packages, reports, repo state)
│   └── jobs.py           in-process async job registry for sync/analyze
├── tests/                pytest suite (unit + API + end-to-end, no external services)
├── tools/smoke_real_repos.py   manual end-to-end run against the real repositories
├── data/                 work dir: data/repos (clones), data/chroma, data/analyzer.db
└── pyproject.toml
```

## Quickstart

```bash
python -m venv .venv && . .venv/Scripts/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                  # then set OPENAI_API_KEY
uvicorn app.main:app --reload
```

First run (clones both repos at depth 1 and indexes everything):

```bash
curl -X POST localhost:8000/api/sync -H 'content-type: application/json' -d '{"full_reindex": true}'
curl localhost:8000/api/jobs/<job_id>
curl localhost:8000/api/packages?status=indexed | head
curl -X POST localhost:8000/api/analyze -H 'content-type: application/json' \
     -d '{"package_name": "nginx", "distro": "ubuntu"}'
curl localhost:8000/api/results/nginx?distro=ubuntu
```

Offline smoke run against the real repositories (hash embeddings + mock LLM, no keys):

```bash
python tools/smoke_real_repos.py --limit 25 --analyze nginx envoy
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/sync` | Pull latest commits and index. `?wait=true` runs inline; otherwise returns a job id |
| `GET` | `/api/packages` | Packages with status (`indexed`, `analyzed`, `error`, …), `status`/`search`/`limit` filters |
| `POST` | `/api/analyze` | Compare the matched doc/script pair; `?background=true` queues it |
| `GET` | `/api/results/{package}` | Stored reports, newest first (`distro`, `limit`, `offset`) |
| `GET` | `/api/results/{package}/latest` | Most recent report for one distro |
| `GET` | `/api/jobs/{job_id}` | Status/result of a background sync or analysis |
| `GET` | `/health` | Backend, collection, index size and indexed commits |

Set `API_TOKEN` to require `Authorization: Bearer <token>` on every `/api/*` call.

## The three analysis layers

1. **Deterministic retrieval** — chunks filtered on `(package_name, distro, source_type)`;
   a package's `unknown`-distro (shared) sections are merged back in, and the script
   version the docs mention is selected (otherwise the newest).
2. **Rule engine** (`RuleEngine`) — set algebra over extracted components:
   documented-but-uninstalled dependencies (with version-variant downgrade to `info`),
   undocumented installs, env-var presence/value drift (shell expansions excluded as
   incomparable), configure/cmake/compiler flags, undocumented s390x patches, and
   documentation-vs-scripts version drift. Cheap, reproducible, evidence-quoting.
3. **LLM review** — Instructor forces `AnalysisReportCore` JSON (`extra="forbid"`).
   Rule findings are injected into the prompt as evidence; findings are merged and
   de-duplicated on `(category, component)` with `detected_by = rule|llm|both`.

`consistency_score` is the more conservative of the two signals: the rule engine's
confidence-weighted exponential decay score, or the model's own score.

## Configuration

See `.env.example`. Highlights:

| Variable | Default | Notes |
| --- | --- | --- |
| `VECTOR_BACKEND` | `chroma` | `chroma` (embedded, persistent) or `qdrant` |
| `EMBEDDING_PROVIDER` | `hash` | `hash` = dependency-free deterministic (dev/CI); `openai` = any OpenAI-compatible `/v1/embeddings` |
| `LLM_PROVIDER` | `openai` | `openai` (or compatible: vLLM/Ollama), `anthropic`, `mock` |
| `LLM_RESPONSE_MODE` | `tools` | `json` for endpoints without tool-calling |
| `DATABASE_URL` | SQLite file | Point at Postgres for a shared deployment |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1400` / `200` | Docs is paragraph-aware, scripts are line-windowed |

## Testing

```bash
pytest -q          # 32 tests, no network, no API keys
```

The suite runs the whole pipeline against an in-memory vector store
(`tests/fakes.py`) and the `mock` LLM provider, including the FastAPI routes,
plus a `docker`-free regression corpus modelled on real repo layout.

## Operational notes

* `POST /api/sync` is single-flight per process: a second call returns `409` with the running job id.
* Indexing is idempotent per package: the package's vectors are replaced, and
  sources that disappeared from the repos are pruned.
* A package that fails to index is recorded with status `error` and does not abort the run.
* Retrieval is capped (`chunk_size`, `MAX_CONTEXT_CHARS`, `RETRIEVAL_TOP_K`) so prompts stay bounded.
* The job registry is in-process: for multiple replicas, move it to Redis + Arq/Celery
  (only `JobRegistry.submit`/`get` need replacing).
* Recommended runtime is CPython 3.10–3.12; ChromaDB pulls `onnxruntime`, whose wheels
  lag the newest Python releases.
