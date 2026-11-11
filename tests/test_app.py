import importlib

import pytest
from fastapi.testclient import TestClient


def client(monkeypatch, mode="none", **env):
    monkeypatch.setenv("FAULT_MODE", mode)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.main as m
    importlib.reload(m)
    return TestClient(m.app)


def test_root_and_probes(monkeypatch):
    c = client(monkeypatch)
    assert c.get("/").json()["fault_mode"] == "none"
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 200


def test_metrics_are_exposed(monkeypatch):
    c = client(monkeypatch)
    c.get("/")
    body = c.get("/metrics").text
    assert "http_requests_total" in body and "http_request_duration_seconds" in body


def test_not_ready_mode_fails_readiness_only(monkeypatch):
    c = client(monkeypatch, "not_ready")
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 503


def test_errors_mode_returns_500(monkeypatch):
    c = client(monkeypatch, "errors", ERROR_RATE="1.0")
    assert c.get("/").status_code == 500
    assert c.get("/healthz").status_code == 200  # probes stay healthy


def test_unknown_mode_falls_back_to_none(monkeypatch):
    c = client(monkeypatch, "explode")
    assert c.get("/").json()["fault_mode"] == "none"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    yield
    monkeypatch.delenv("FAULT_MODE", raising=False)
