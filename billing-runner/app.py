"""Billing runner HTTP surface. /run can never return a bare 500."""
import os, datetime, logging
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from services import Services
from runner import run_billing

VERSION = "r6-nullphone-dryrun-version"
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
    return {"version": VERSION}

@app.get("/config-check")
def config_check(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    required = ["VERDICT_BASE","BILLING_BASE","WEBHOOKS_BASE","NMI_ENDPOINT","INTERNAL_API_KEY","NMI_SECURITY_KEY"]
    cfg = {k: ("set" if os.environ.get(k) else "MISSING") for k in required}
    for k in ["VERDICT_BASE","BILLING_BASE","WEBHOOKS_BASE","NMI_ENDPOINT"]:
        cfg[k] = os.environ.get(k) or "MISSING"
    return {"ok": all(os.environ.get(k) for k in required), "version": VERSION, "config": cfg}

@app.post("/run")
def run(date: str | None = None,
        dry: bool = Query(default=False),
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
    return summary
