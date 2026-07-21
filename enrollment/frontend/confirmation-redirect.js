/*
 * Vance Credit — enrollment confirmation redirect (pricing-page modal).
 *
 * PASTE this into the pricing-page enrollment modal script, AFTER
 * card-brand-guard.js. It supplies the `window.finishEnroll(response)` that the
 * brand guard calls with the Collect.js tokenize response once the card brand is
 * accepted (see card-brand-guard.js -> CollectJS callback).
 *
 * What it does: POST the token + form fields to the enrollment service's
 * POST /enroll, then send the browser to the server-rendered onboarding page at
 *   <ENROLLMENT_BASE>/enroll/confirmation/<client_id>
 * instead of rendering a confirmation screen inline. That page is the single
 * source of truth for what the customer is told to do next.
 *
 * SETUP (two things to adapt):
 *   1. ENROLLMENT_BASE below -> the enrollment service's public Railway URL.
 *   2. FIELD_SELECTORS below -> your form's DOM.
 * Everything else is self-contained.
 */
(function () {
  'use strict';

  // ---- 1. the enrollment service's public base URL -------------------------
  const ENROLLMENT_BASE = 'https://<enrollment>.up.railway.app';

  // ---- 2. SELECTORS — adapt to the pricing page DOM ------------------------
  // Only plan_tier and email are required by POST /enroll; the rest improve AVS
  // on every future charge, so send them when the form collects them.
  const FIELD_SELECTORS = {
    plan_tier: '[name="plan_tier"]',
    email: '[name="email"]',
    phone: '[name="phone"]',
    name: '[name="name"]',
    address1: '[name="address1"]',
    address2: '[name="address2"]',
    city: '[name="city"]',
    state: '[name="state"]',
    zip: '[name="zip"]',
  };

  const GENERIC_ERROR =
    'We could not complete your enrollment. Your card was not charged. Please try again.';

  function readField(sel) {
    const el = document.querySelector(sel);
    return el && typeof el.value === 'string' ? el.value.trim() : '';
  }

  function collectEnrollFields() {
    const out = {};
    Object.keys(FIELD_SELECTORS).forEach(function (key) {
      const v = readField(FIELD_SELECTORS[key]);
      if (v) out[key] = v;
    });
    return out;
  }

  // Surface a failure using the brand guard's inline error target if it exists,
  // so we do not invent a second error style on the page.
  function showEnrollError(msg) {
    const el = document.querySelector('#card-error');
    if (el) { el.textContent = msg; return; }
    window.alert(msg);
  }

  function confirmationUrl(clientId) {
    return ENROLLMENT_BASE.replace(/\/+$/, '') +
           '/enroll/confirmation/' + encodeURIComponent(clientId);
  }

  /*
   * Called by card-brand-guard.js with the Collect.js tokenize response.
   * `response.token` is the opaque Collect.js payment token — the PAN never
   * touches this page or our servers.
   */
  window.finishEnroll = function (response) {
    const token = response && response.token;
    if (!token) { showEnrollError(GENERIC_ERROR); return; }

    const payload = collectEnrollFields();
    payload.collect_js_token = token;

    fetch(ENROLLMENT_BASE.replace(/\/+$/, '') + '/enroll', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(function (res) {
        return res.json().then(function (data) { return { ok: res.ok, data: data }; });
      })
      .then(function (result) {
        // Response shape is EnrollOut: {ok, client_id, plan_tier, vaulted, charged}.
        if (result.ok && result.data && result.data.client_id) {
          window.location.assign(confirmationUrl(result.data.client_id));
          return;
        }
        // 402 (card declined / vault failed) and 422 (brand or plan rejected) carry
        // a human-readable `detail` from the service; show it verbatim.
        const detail = result.data && result.data.detail;
        showEnrollError(typeof detail === 'string' ? detail : GENERIC_ERROR);
      })
      .catch(function () {
        showEnrollError(GENERIC_ERROR);
      });
  };
})();
