#!/usr/bin/env python
"""Exercise the HTTP API end to end with an in-process TestClient.

Uses the deterministic hash embedder and the mock LLM by default, so it needs no
API keys. The repositories must already be available under ``data/repos``
(``python tools/smoke_real_repos.py --no-git`` creates them).

    python tools/api_smoke.py --package nginx --distro ubuntu
    python tools/api_smoke.py --sync --package nginx --sync-limit 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", default="nginx", help="Package to analyze")
    parser.add_argument("--distro", default="ubuntu")
    parser.add_argument("--sync", action="store_true", help="Also run POST /api/sync inline (clones/pulls repos)")
    parser.add_argument(
        "--sync-packages",
        default="",
        help="Comma-separated package subset to index during --sync (default: all)",
    )
    parser.add_argument("--llm", default="mock", choices=["mock", "openai", "anthropic"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # Environment must be set *before* app.config caches its settings.
    os.environ["EMBEDDING_PROVIDER"] = "hash"
    os.environ["LLM_PROVIDER"] = args.llm

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import AnalysisStatus

    with TestClient(app) as client:
        health = client.get("/health")
        print("GET /health ->", health.status_code, json.dumps(health.json(), indent=2)[:400])
        assert health.status_code == 200, health.text

        if args.sync:
            subset = [name.strip() for name in args.sync_packages.split(",") if name.strip()] or None
            response = client.post(
                "/api/sync",
                params={"wait": True},
                json={"full_reindex": False, "packages": subset, "prune": False},
            )
            print("POST /api/sync (wait) ->", response.status_code)
            print(json.dumps(response.json().get("stats") or {}, indent=2)[:600])
            assert response.status_code == 202, response.text

        listing = client.get("/api/packages", params={"limit": 5})
        print(f"GET /api/packages -> {listing.status_code}, total={listing.json()['total']}")
        indexed = client.get("/api/packages", params={"status": AnalysisStatus.INDEXED.value, "limit": 3})
        print("  indexed sample:", [item["match_key"] for item in indexed.json()["packages"]])

        analysis = client.post(
            "/api/analyze",
            json={"package_name": args.package, "distro": args.distro, "force": True},
        )
        print(f"POST /api/analyze ({args.package}/{args.distro}) -> {analysis.status_code}")
        if analysis.status_code != 200:
            print("  detail:", analysis.json())
            return 1
        report = analysis.json()["report"]
        print(
            f"  score={report['consistency_score']} findings={len(report['discrepancies'])} "
            f"model={report['model']} doc={report['doc']['path']} script={report['script']['path']}"
        )
        for finding in report["discrepancies"][:5]:
            print(f"    [{finding['severity']:<8}] {finding['category']:<24} {finding['title']}")

        results = client.get(f"/api/results/{args.package}", params={"distro": args.distro})
        print(f"GET /api/results/{args.package} -> {results.status_code}, total={results.json()['total']}")
        latest = client.get(f"/api/results/{args.package}/latest", params={"distro": args.distro})
        print(f"GET /api/results/{args.package}/latest -> {latest.status_code}")

        background = client.post(
            "/api/analyze",
            params={"background": True},
            json={"package_name": args.package, "distro": args.distro},
        )
        job_id = background.json().get("job_id")
        job = client.get(f"/api/jobs/{job_id}")
        print(f"POST /api/analyze (background) -> {background.status_code}, job {job_id} state={job.json()['state']}")
    print("API smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
