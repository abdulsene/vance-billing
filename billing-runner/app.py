"""Billing runner HTTP surface. /run can never return a bare 500."""
import os, datetime, logging, urllib.request
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from services import Services
from runner import run_billing

VERSION = "r7-orphan-alert-preflight"   # bump on every deploy to verify what's live
logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Vance Billing Runner")

def _auth(x_api_key):
    expected = os.environ.get("INTERNAL_API_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing API key")

@app.get("/health")
def health():
    return {"ok": True, "service": "billing-runner", "version": VERSION}

@app.get("/version")
def version():
    return {"version": VERSION}   # public: confirm the deployed build

@app.get("/config-check")
def config_check(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    required = ["VERDICT_BASE","BILLING_BASE","WEBHOOKS_BASE","NMI_ENDPOINT","INTERNAL_API_KEY","NMI_SECURITY_KEY"]
    cfg = {k: ("set" if os.environ.get(k) else "MISSING") for k in required}
    for k in ["VERDICT_BASE","BILLING_BASE","WEBHOOKS_BASE","NMI_ENDPOINT"]:
        cfg[k] = os.environ.get(k) or "MISSING"
    return {"ok": all(os.environ.get(k) for k in required), "version": VERSION, "config": cfg}

REQUIRED_ENV = ["VERDICT_BASE","BILLING_BASE","WEBHOOKS_BASE","NMI_ENDPOINT","INTERNAL_API_KEY","NMI_SECURITY_KEY"]
SECRET_ENV   = {"INTERNAL_API_KEY","NMI_SECURITY_KEY"}   # never echoed back, only set/MISSING

# (label, env var, health path) - each service namespaces its own health route.
PROBE_TARGETS = [
    ("verdict-service /parser/health",  "VERDICT_BASE",  "/parser/health"),
    ("billing-api /billing/health",     "BILLING_BASE",  "/billing/health"),
    ("webhooks /webhooks/health",       "WEBHOOKS_BASE", "/webhooks/health"),
]

def _probe(name, base, path):
    """GET <base><path>. Any failure is a red row, never an exception.

    Each service namespaces its own health route (/parser/health, /billing/health,
    /webhooks/health) - a bare /health 404s and shows up as a false failure."""
    if not base:
        return {"check": name, "kind": "probe", "required": True, "ok": False,
                "detail": "MISSING - base URL not set, cannot probe"}
    url = f"{base.rstrip('/')}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as r:
            code = r.getcode()
        return {"check": name, "kind": "probe", "required": True, "ok": code == 200,
                "url": url, "detail": f"HTTP {code}"}
    except Exception as e:
        return {"check": name, "kind": "probe", "required": True, "ok": False,
                "url": url, "detail": f"{type(e).__name__}: {e}"}

@app.get("/preflight")
def preflight(x_api_key: str | None = Header(default=None)):
    """Readiness board: is every env var set and is every dependency answering?
    Call this before trusting a cron run. Cannot 500 - a dead dependency is a red row."""
    _auth(x_api_key)
    checks = []
    for k in REQUIRED_ENV:
        v = os.environ.get(k)
        checks.append({"check": k, "kind": "env", "required": True, "ok": bool(v),
                       "detail": ("set" if k in SECRET_ENV else v) if v else "MISSING"})
    for label, env, path in PROBE_TARGETS:
        checks.append(_probe(label, os.environ.get(env), path))

    ops = os.environ.get("ORPHAN_ALERT_PHONE")   # informational: does not gate readiness
    checks.append({"check": "orphan alert", "kind": "info", "required": False, "ok": bool(ops),
                   "detail": f"ORPHAN_ALERT_PHONE set ({ops})" if ops
                             else "ORPHAN_ALERT_PHONE not set - charge-orphans log only, no SMS"})

    ready = all(c["ok"] for c in checks if c["required"])
    logging.info("preflight ready=%s failing=%s", ready,
                 [c["check"] for c in checks if c["required"] and not c["ok"]])
    return {"ready": ready, "version": VERSION, "checks": checks}

@app.post("/run")
def run(date: str | None = None,
        dry: bool = Query(default=False, description="read-only: report would_charge, never charges"),
        x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    d = date or datetime.date.today().isoformat()
    try:
        summary = run_billing(Services(), date=d, dry=dry)
    except Exception as e:
        logging.exception("run_billing crashed")
        return JSONResponse(status_code=500, content={
            "ok": False, "version": VERSION, "stage": "startup",
            "error": f"{type(e).__name__}: {e}",
            "hint": "call GET /config-check to see which env var is missing/wrong"})
    summary["version"] = VERSION
    logging.info("run %s dry=%s: charged=%d would=%d skipped=%d declined=%d orphans=%d errors=%d",
                 d, dry, len(summary["charged"]), len(summary["would_charge"]),
                 len(summary["skipped"]), len(summary["declined"]),
                 len(summary["orphans"]), len(summary["errors"]))
    return summary
