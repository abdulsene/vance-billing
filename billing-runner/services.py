"""Real adapter: talks to the four deployed FastAPI services + NMI. Stdlib only."""
import os, json, urllib.parse, urllib.request, logging
from nmi import vault_sale
log = logging.getLogger("services")

class Services:
    def __init__(self):
        self.verdict = _req_env("VERDICT_BASE")     # https://vance-billing-production.up.railway.app
        self.billing = _req_env("BILLING_BASE")     # https://delightful-perfection-...up.railway.app
        self.webhooks = _req_env("WEBHOOKS_BASE")   # https://natural-commitment-...up.railway.app
        self.api_key = _req_env("INTERNAL_API_KEY")
        self.nmi_endpoint = _req_env("NMI_ENDPOINT")           # ecrypt.transactiongateway.com
        self.nmi_key = _req_env("NMI_SECURITY_KEY")

    def _headers(self, json_body=False):
        h = {"X-API-Key": self.api_key}
        if json_body: h["Content-Type"] = "application/json"
        return h

    def get_due(self, date):
        return _get(f"{self.billing}/billing/due?date={urllib.parse.quote(date)}", self._headers())
    def get_verdict(self, cid, cycle):
        q = urllib.parse.urlencode({"client_id": cid, "cycle": cycle})
        return _get(f"{self.verdict}/parser/verdict?{q}", self._headers())
    def vault_sale(self, vault, amount, *, orderid):
        return vault_sale(self.nmi_endpoint, self.nmi_key, vault, amount, orderid=orderid)
    def mark_billed(self, cid, cycle, txn):
        return _post_json(f"{self.billing}/billing/mark-billed",
                          {"client_id": cid, "cycle": cycle, "txn_id": txn}, self._headers(True))
    def commit(self, cid, cycle):
        return _post_json(f"{self.verdict}/parser/verdict/commit",
                          {"client_id": cid, "cycle": cycle}, self._headers(True))
    def create_invoice(self, payload):
        return _post_json(f"{self.webhooks}/crc/invoice", payload, self._headers(True))
    def notify(self, payload):
        return _post_json(f"{self.webhooks}/notify", payload, self._headers(True))

def _req_env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set — billing-runner cannot start")
    return v
def _get(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())
def _post_json(url, obj, headers):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
        try: return json.loads(body)
        except Exception: return {"raw": body}
