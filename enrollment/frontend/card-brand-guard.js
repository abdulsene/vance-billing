/*
 * Vance Credit — enrollment card-brand guard (pricing-page modal).
 *
 * PASTE this into the pricing-page enrollment modal script. It restricts the card
 * form to Visa + Mastercard (Amex/Discover are OFF pending processor approval) via
 * NMI Collect.js, and mirrors the backend backstop (enroll_core.is_accepted_brand)
 * and the unit-tested logic in brand_guard.mjs. To enable a brand later, add it to
 * ACCEPTED_BRANDS below (and the two backend/module lists).
 *
 * Adapt the three SELECTORS to your DOM; everything else is self-contained.
 */
(function () {
  'use strict';

  // ---- one-line to expand later --------------------------------------------
  const ACCEPTED_BRANDS = ['visa', 'mastercard'];

  const UNSUPPORTED_CARD_MESSAGE =
    'We currently accept Visa and Mastercard only. Please use a different card.';
  const ACCEPTED_CARDS_TEXT = 'We accept Visa and Mastercard.';

  // ---- SELECTORS — adapt to the pricing page DOM ---------------------------
  const CARD_LABEL_SELECTOR = '[data-card-details-label]'; // the "CARD DETAILS" label
  const CCNUMBER_FIELD_SELECTOR = '#ccnumber';             // Collect.js ccnumber mount
  const CARD_ERROR_SELECTOR = '#card-error';               // inline error target (created if absent)

  const ALIASES = {
    visa: 'visa',
    mastercard: 'mastercard', 'master card': 'mastercard', master: 'mastercard', mc: 'mastercard',
    amex: 'amex', 'american express': 'amex', americanexpress: 'amex',
    discover: 'discover',
  };
  function normalizeBrand(raw) {
    if (!raw) return '';
    return ALIASES[String(raw).trim().toLowerCase()] || String(raw).trim().toLowerCase();
  }
  function isAcceptedBrand(raw) {
    return ACCEPTED_BRANDS.includes(normalizeBrand(raw));
  }

  // Module state: the last brand Collect.js reported, and whether it's blocked.
  let detectedBrand = '';
  let brandBlocked = false;

  // ---- visible accepted-cards text under CARD DETAILS ----------------------
  function renderAcceptedCardsText() {
    const label = document.querySelector(CARD_LABEL_SELECTOR);
    if (!label || label.parentNode.querySelector('.vc-accepted-cards')) return;
    const note = document.createElement('small');
    note.className = 'vc-accepted-cards';
    note.textContent = ACCEPTED_CARDS_TEXT;
    note.style.cssText = 'display:block;color:#6b7280;font-size:.8rem;margin-top:2px;';
    label.insertAdjacentElement('afterend', note);
  }

  // ---- inline error + field invalid styling --------------------------------
  function errorEl() {
    let el = document.querySelector(CARD_ERROR_SELECTOR);
    if (!el) {
      const field = document.querySelector(CCNUMBER_FIELD_SELECTOR);
      el = document.createElement('div');
      el.id = (CARD_ERROR_SELECTOR || '#card-error').replace(/^#/, '');
      el.setAttribute('role', 'alert');
      el.style.cssText = 'color:#b91c1c;font-size:.85rem;margin-top:6px;';
      if (field && field.parentNode) field.parentNode.appendChild(el);
      else document.body.appendChild(el);
    }
    return el;
  }
  function showCardError(msg) {
    errorEl().textContent = msg;
    const field = document.querySelector(CCNUMBER_FIELD_SELECTOR);
    if (field) field.classList.add('vc-card-invalid');
  }
  function clearCardError() {
    const el = document.querySelector(CARD_ERROR_SELECTOR);
    if (el) el.textContent = '';
    const field = document.querySelector(CCNUMBER_FIELD_SELECTOR);
    if (field) field.classList.remove('vc-card-invalid');
  }

  // ---- the guard: called whenever Collect.js reports a card type -----------
  function handleBrand(brand) {
    detectedBrand = normalizeBrand(brand);
    if (!detectedBrand) { brandBlocked = false; clearCardError(); return; }
    if (isAcceptedBrand(detectedBrand)) { brandBlocked = false; clearCardError(); }
    else { brandBlocked = true; showCardError(UNSUPPORTED_CARD_MESSAGE); }
  }

  // ---- Collect.js wiring ----------------------------------------------------
  // Merge these into your existing CollectJS.configure({...}). Collect.js reports
  // the brand via the ccnumber field's validation and in the tokenize response's
  // `card.type`, so we hook both.
  window.vcCardBrandGuardConfig = {
    // Real-time: Collect.js passes the detected type on the ccnumber field.
    validationCallback: function (field, status, message) {
      if (field === 'ccnumber') {
        // Some Collect.js builds encode the brand in `message`; guard defensively.
        const brand = (window.CollectJS && CollectJS.cardType) || message || '';
        handleBrand(brand);
      }
    },
    // Authoritative: the tokenize response includes card.type.
    callback: function (response) {
      const brand = response && response.card && response.card.type;
      handleBrand(brand);
      if (brandBlocked) {
        showCardError(UNSUPPORTED_CARD_MESSAGE);
        return; // do NOT proceed to POST /enroll with an unsupported brand
      }
      if (typeof window.finishEnroll === 'function') window.finishEnroll(response);
    },
  };

  // ---- submit guard: never startPaymentRequest on an unsupported brand ------
  // Wrap your existing submitEnroll so the guard runs first.
  window.guardSubmitEnroll = function (originalSubmit) {
    return function () {
      if (brandBlocked || (detectedBrand && !isAcceptedBrand(detectedBrand))) {
        showCardError(UNSUPPORTED_CARD_MESSAGE);
        return; // block: do not call CollectJS.startPaymentRequest()
      }
      return originalSubmit.apply(this, arguments);
    };
  };

  // Expose the pure helpers for reuse/testing parity.
  window.vcBrandGuard = { ACCEPTED_BRANDS, isAcceptedBrand, normalizeBrand, UNSUPPORTED_CARD_MESSAGE };

  if (document.readyState !== 'loading') renderAcceptedCardsText();
  else document.addEventListener('DOMContentLoaded', renderAcceptedCardsText);
})();
