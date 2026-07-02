// Pure, dependency-free parser for NMI's x-www-form-urlencoded response.
//
// This is the SAME logic embedded in the billing gate's "Parse NMI Response"
// Code node (vance-billing-gate.n8n.json). n8n Code nodes run in a Node sandbox
// with NO browser globals — URLSearchParams/atob/btoa are undefined — so parsing
// is done by hand here and in the node. Keep the two in sync.
export function parseNmi(raw) {
  const dec = (s) => { try { return decodeURIComponent(s); } catch (e) { return s; } };
  const p = {};
  for (const kv of String(raw).split('&')) {
    const i = kv.indexOf('=');
    if (i < 0) continue;                       // skip fragments without '='
    const k = dec(kv.slice(0, i));
    const v = dec(kv.slice(i + 1).replace(/\+/g, ' '));  // '+' means space
    p[k] = v;
  }
  return {
    response: p.response,                      // "1"=approved, "2"=decline, "3"=error
    responsetext: p.responsetext,
    authcode: p.authcode,
    transactionid: p.transactionid,
    avsresponse: p.avsresponse,
    cvvresponse: p.cvvresponse,
    response_code: p.response_code,
  };
}
