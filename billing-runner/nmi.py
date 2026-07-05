"""NMI vault-sale call + dependency-free response parser (no browser globals)."""
import urllib.parse, urllib.request, logging
log = logging.getLogger("nmi")

def parse_nmi(raw: str) -> dict:
    p = {}
    for kv in str(raw or "").split("&"):
        if not kv:
            continue
        i = kv.find("=")
        k = kv if i == -1 else kv[:i]
        v = "" if i == -1 else kv[i+1:]
        try: key = urllib.parse.unquote_plus(k)
        except Exception: key = k
        try: val = urllib.parse.unquote_plus(v)
        except Exception: val = v
        p[key] = val
    response = p.get("response", "")
    return {
        "response": response,
        "approved": response == "1",
        "responsetext": p.get("responsetext", ""),
        "authcode": p.get("authcode", ""),
        "transactionid": p.get("transactionid", ""),
        "avsresponse": p.get("avsresponse", ""),
        "cvvresponse": p.get("cvvresponse", ""),
        "response_code": p.get("response_code", ""),
        "raw": str(raw or ""),
    }

def vault_sale(endpoint: str, security_key: str, vault_id: str, amount, *, orderid: str, opener=None) -> str:
    """Charge a stored card via NMI Customer Vault. Returns the raw response body.
    orderid = client|cycle gives NMI-side duplicate protection (idempotency)."""
    body = urllib.parse.urlencode({
        "security_key": security_key,
        "customer_vault_id": vault_id,
        "amount": f"{float(amount):.2f}",
        "type": "sale",
        "orderid": orderid,          # NMI duplicate-transaction guard
    }).encode()
    url = f"https://{endpoint}/api/transact.php"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    _open = opener or urllib.request.urlopen
    with _open(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")
