import os
from fastapi import Header, HTTPException

def require_api_key(x_api_key: str | None = Header(default=None)):
    """Fail-open when INTERNAL_API_KEY is unset (staged rollout); enforce when set."""
    expected = os.environ.get("INTERNAL_API_KEY")
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
