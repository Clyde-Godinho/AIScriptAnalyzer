from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import Services, app


@pytest.fixture()
def client(services: Services, corpus: dict[str, str]) -> TestClient:
    """A TestClient with pre-wired services (lifespan is skipped on purpose)."""
    app.state.services = services
    app.state.startup_error = None
    return TestClient(app)


def test_health_reports_index_state(client: TestClient, corpus: dict[str, str]) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["indexed_chunks"] > 0
    assert body["packages"] == 2
    assert body["llm_provider"] == "mock"


def test_packages_listing_and_filters(client: TestClient, corpus: dict[str, str]) -> None:
    response = client.get("/api/packages")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["match_key"] for item in body["packages"]} == {"example::ubuntu", "example::rhel"}
    assert all(item["status"] == "indexed" for item in body["packages"])

    assert client.get("/api/packages", params={"status": "analyzed"}).json()["total"] == 0
    assert client.get("/api/packages", params={"search": "exam"}).json()["total"] == 2
    assert client.get("/api/packages", params={"status": "bogus"}).status_code == 422


def test_analyze_then_fetch_results(client: TestClient, corpus: dict[str, str]) -> None:
    response = client.post("/api/analyze", json={"package_name": "example", "distro": "ubuntu"})
    assert response.status_code == 200, response.text
    report = response.json()["report"]
    assert report["match_key"] == "example::ubuntu"
    assert report["discrepancies"], "the fixture pair is deliberately inconsistent"
    assert report["doc"]["source_type"] == "doc"
    assert report["script"]["source_type"] == "script"
    assert 0 <= report["consistency_score"] <= 100

    results = client.get("/api/results/example", params={"distro": "ubuntu"})
    assert results.status_code == 200
    body = results.json()
    assert body["total"] == 1
    assert body["reports"][0]["report_id"] == report["report_id"]

    latest = client.get("/api/results/example/latest", params={"distro": "rhel"})
    assert latest.status_code == 404  # no rhel report stored yet

    assert client.get("/api/results/unknown-package").status_code == 404


def test_analyze_unknown_package_is_404(client: TestClient, corpus: dict[str, str]) -> None:
    response = client.post("/api/analyze", json={"package_name": "nope", "distro": "ubuntu"})
    assert response.status_code == 404
    assert "run post /api/sync" in response.json()["detail"].lower()


def test_analyze_validation_error(client: TestClient) -> None:
    response = client.post("/api/analyze", json={"package_name": "example", "distro": "plan9"})
    assert response.status_code == 422


def test_sync_wait_true_returns_ingestion_counters(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        services.pipeline,
        "run",
        lambda **kwargs: {"packages_indexed": 3, "chunks_indexed": 42, "kwargs": sorted(kwargs)},
    )
    response = client.post("/api/sync", params={"wait": True}, json={"full_reindex": False})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "succeeded"
    assert body["stats"]["chunks_indexed"] == 42
    assert body["stats"]["kwargs"] == ["full_reindex", "packages", "prune"]


def test_sync_background_returns_a_job(client: TestClient, services: Services) -> None:
    response = client.post("/api/sync", json={})
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert job_id

    status_response = client.get(f"/api/jobs/{job_id}")
    assert status_response.status_code == 200
    assert status_response.json()["kind"] == "sync"
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_analyze_background_returns_a_job(client: TestClient, corpus: dict[str, str]) -> None:
    response = client.post(
        "/api/analyze",
        params={"background": True},
        json={"package_name": "example", "distro": "ubuntu"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["report"] is None
    assert body["job_id"]


def test_bearer_token_is_enforced(
    client: TestClient, services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services.settings, "api_token", "s3cret")
    assert client.get("/api/packages").status_code == 401
    assert client.get("/api/packages", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.get("/api/packages", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    )
