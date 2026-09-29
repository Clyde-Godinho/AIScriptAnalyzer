# s390x Consistency Dashboard

Next.js 14 (App Router) + Tailwind CSS front end for the AI-Powered
Documentation–Script Consistency Analyzer.

## Run

```bash
# 1. start the FastAPI backend (repo root)
uvicorn app.main:app --reload            # http://localhost:8000

# 2. start the dashboard
cd frontend
npm install
BACKEND_ORIGIN=http://localhost:8000 npm run dev   # http://localhost:3000
```

`next.config.mjs` proxies `/api/*` to `BACKEND_ORIGIN` (default
`http://localhost:8000`), so the browser talks to same-origin URLs and CORS is
not an issue.

## Views

| View | File | Purpose |
| --- | --- | --- |
| Package Matrix | `src/components/PackageMatrix.tsx` | Searchable catalog with health badges |
| Unified Diff Viewer | `src/components/DiffViewer.tsx` | Side-by-side doc steps vs script commands |
| Discrepancy Drawer | `src/components/DiscrepancyDrawer.tsx` | JSON findings detail |
| Top bar | `src/components/TopBar.tsx` | Backend health pill, `POST /api/sync` button |

## Backend endpoints used

- `GET  /api/packages` — package catalog (search, status filter)
- `POST /api/analyze` — run the LLM comparison for one package
- `POST /api/sync` — re-index the doc/script repositories
- `GET  /api/jobs/{id}` — background job polling
- `GET  /api/results/{package}/latest` — last stored report
- `GET  /health` — header health pill
