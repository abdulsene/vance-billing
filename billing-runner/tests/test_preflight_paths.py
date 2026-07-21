"""Probe-URL tests for /preflight.

Each dependency namespaces its own health route (/parser/health, /billing/health,
/webhooks/health). A bare <base>/health 404s, which /preflight reported as a red row
against a service that was actually up - the false failure this file exists to prevent.

_probe is tested directly: no HTTP server, no httpx, no TestClient.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import app as appmod


class FakeResponse:
    def __init__(self, code=200): self._code = code
    def getcode(self): return self._code
    def __enter__(self): return self
    def __exit__(self, *a): return False


def capture(monkeypatch, code=200):
    """Swap urlopen for a recorder; returns the list it appends (url, timeout) to."""
    seen = []
    def fake_urlopen(req, timeout=None):
        seen.append((req.full_url, timeout))
        return FakeResponse(code)
    monkeypatch.setattr(appmod.urllib.request, "urlopen", fake_urlopen)
    return seen


EXPECTED = {
    "VERDICT_BASE":  "/parser/health",
    "BILLING_BASE":  "/billing/health",
    "WEBHOOKS_BASE": "/webhooks/health",
}


def test_each_service_is_probed_at_its_own_health_path(monkeypatch):
    for label, env, path in appmod.PROBE_TARGETS:
        seen = capture(monkeypatch)
        row = appmod._probe(label, "https://svc.example.com", path)
        assert seen[0][0] == f"https://svc.example.com{EXPECTED[env]}", label
        assert row["ok"] is True


def test_no_service_is_probed_at_bare_health(monkeypatch):
    # The r7 bug: every probe hit <base>/health, which no service serves.
    for label, env, path in appmod.PROBE_TARGETS:
        seen = capture(monkeypatch)
        appmod._probe(label, "https://svc.example.com", path)
        assert seen[0][0] != "https://svc.example.com/health", f"{label} still probes bare /health"


def test_probe_targets_cover_the_three_dependencies():
    assert [t[1] for t in appmod.PROBE_TARGETS] == ["VERDICT_BASE", "BILLING_BASE", "WEBHOOKS_BASE"]
    assert [t[2] for t in appmod.PROBE_TARGETS] == ["/parser/health", "/billing/health", "/webhooks/health"]


def test_trailing_slash_on_base_does_not_double_up(monkeypatch):
    seen = capture(monkeypatch)
    appmod._probe("verdict", "https://svc.example.com/", "/parser/health")
    assert seen[0][0] == "https://svc.example.com/parser/health"


def test_probe_uses_8s_timeout(monkeypatch):
    seen = capture(monkeypatch)
    appmod._probe("verdict", "https://svc.example.com", "/parser/health")
    assert seen[0][1] == 8


def test_non_200_is_a_red_row_not_ok(monkeypatch):
    capture(monkeypatch, code=404)
    row = appmod._probe("verdict", "https://svc.example.com", "/parser/health")
    assert row["ok"] is False and "404" in row["detail"]


def test_exception_is_a_red_row_carrying_the_error(monkeypatch):
    def boom(req, timeout=None): raise OSError("connection refused")
    monkeypatch.setattr(appmod.urllib.request, "urlopen", boom)
    row = appmod._probe("verdict", "https://svc.example.com", "/parser/health")
    assert row["ok"] is False
    assert "OSError" in row["detail"] and "connection refused" in row["detail"]
    assert row["url"].endswith("/parser/health")


def test_missing_base_is_red_and_never_probes(monkeypatch):
    seen = capture(monkeypatch)
    row = appmod._probe("verdict", None, "/parser/health")
    assert row["ok"] is False and row["detail"].startswith("MISSING")
    assert seen == []
