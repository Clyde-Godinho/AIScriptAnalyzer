#!/usr/bin/env python
"""Smoke run against the *real* linux-on-ibm-z repositories.

Clones/pulls ``docs.wiki`` and ``scripts`` into ``data/repos`` (depth 1), reports
how many wiki pages paired with build scripts, indexes a subset and optionally
runs analyses. Uses the deterministic hash embedder and the mock LLM by default,
so it needs no API keys.

    python tools/smoke_real_repos.py --limit 25 --analyze nginx envoy
    python tools/smoke_real_repos.py --no-git --limit 5 --packages nginx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, configure_logging  # noqa: E402
from app.main import build_services  # noqa: E402
from app.models import Distro, normalize_distro  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--packages", nargs="*", help="Only index these package names")
    parser.add_argument("--analyze", nargs="*", help="Analyze these package names after indexing")
    parser.add_argument("--distro", default="ubuntu", help="Distro used for the analyses")
    parser.add_argument("--limit", type=int, default=0, help="Index at most N matched packages")
    parser.add_argument("--no-git", action="store_true", help="Skip fetch/pull (repos must exist)")
    parser.add_argument("--full", action="store_true", help="Reset the collection before indexing")
    parser.add_argument("--json", action="store_true", help="Print the reports as JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings(
        vector_backend="chroma",
        embedding_provider="hash",
        llm_provider="mock",
    )
    settings.ensure_directories()
    configure_logging("INFO")
    services = build_services(settings)
    pipeline = services.pipeline

    if args.no_git:
        for name, path in (
            ("docs_wiki", pipeline.wiki_dir),
            ("docs", pipeline.docs_dir),
            ("scripts", pipeline.scripts_dir),
        ):
            services.repository.set_repo_state(name, str(path), pipeline.git.head(path))
    else:
        checkouts = pipeline.sync_repositories()
        print("repositories:", {name: (checkout.head or "")[:10] for name, checkout in checkouts.items()})

    discovered = pipeline.discover()
    matched = [item for item in discovered if item.doc_path and item.script_paths]
    docs_only = [item for item in discovered if item.doc_path and not item.script_paths]
    scripts_only = [item for item in discovered if item.script_paths and not item.doc_path]
    print(
        f"discovered {len(discovered)} packages: {len(matched)} matched pairs, "
        f"{len(docs_only)} docs without scripts, {len(scripts_only)} scripts without docs"
    )
    if docs_only:
        print("  docs without scripts:", ", ".join(item.package_name for item in docs_only[:10]))
    if scripts_only:
        print("  scripts without docs:", ", ".join(item.package_name for item in scripts_only[:10]))
    print("  sample matches:", ", ".join(f"{item.package_name}({len(item.versions)}v)" for item in matched[:8]))

    selection = matched
    if args.packages:
        wanted = {name.lower() for name in args.packages}
        selection = [item for item in matched if item.package_name.lower() in wanted or item.normalized_name in wanted]
    if args.limit:
        selection = selection[: args.limit]

    if selection:
        stats = pipeline.index(selection, full_reindex=args.full)
        print("indexed:", json.dumps(stats.to_dict(), indent=2, default=str)[:1200])

    distro: Distro = normalize_distro(args.distro)
    for name in args.analyze or []:
        report = asyncio.run(services.analyzer.analyze(name, distro, force=True))
        print(
            f"\n=== {report.package_name} / {report.distro.value} === "
            f"score={report.consistency_score} findings={len(report.discrepancies)} "
            f"model={report.model} {report.duration_ms}ms"
        )
        print("summary:", report.summary)
        for finding in report.discrepancies[:8]:
            print(f"  [{finding.severity.value:<8}] {finding.category.value:<24} {finding.title}")
        if report.notes:
            print("notes:", report.notes)
        if args.json:
            print(json.dumps(report.model_dump(mode="json"), indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
