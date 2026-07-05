"""HTTP surface for the billing runner: authenticated manual run + health."""
import os, datetime, logging
from fastapi import FastAPI, Header, HTTPException
from services import Services
from runner import run_billing

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Vance Billing Runner")

def _auth(x_api_key):
    expected = os.environ.get("INTERNAL_API_KEY")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing API key")

@app.get("/health")
def health():
    return {"ok": True, "service": "billing-runner"}

@app.post("/run")
def run(date: str | None = None, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    d = date or datetime.date.today().isoformat()
    summary = run_billing(Services(), date=d)
    logging.info("billing run %s: charged=%d skipped=%d declined=%d orphans=%d",
                 d, len(summary["charged"]), len(summary["skipped"]),
                 len(summary["declined"]), len(summary["orphans"]))
    return summary
