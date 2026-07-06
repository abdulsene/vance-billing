// Pure card-brand-guard logic for the enrollment card form.
//
// This mirrors the inline logic in `card-brand-guard.js` (the paste target for the
// vancecredit.com pricing-page modal) AND the backend `enroll_core.is_accepted_brand`.
// Keep the three in sync. Amex + Discover are OFF pending processor approval — to
// enable a brand, add it to ACCEPTED_BRANDS here, in card-brand-guard.js, and to
// enroll_core.ACCEPTED_CARD_BRANDS.

export const ACCEPTED_BRANDS = ['visa', 'mastercard'];   // one-line to expand later

export const UNSUPPORTED_CARD_MESSAGE =
  'We currently accept Visa and Mastercard only. Please use a different card.';

export const ACCEPTED_CARDS_TEXT = 'We accept Visa and Mastercard.';

const ALIASES = {
  visa: 'visa',
  mastercard: 'mastercard', 'master card': 'mastercard', master: 'mastercard', mc: 'mastercard',
  amex: 'amex', 'american express': 'amex', americanexpress: 'amex',
  discover: 'discover',
};

export function normalizeBrand(raw) {
  if (!raw) return '';
  const b = String(raw).trim().toLowerCase();
  return ALIASES[b] || b;
}

export function isAcceptedBrand(raw) {
  return ACCEPTED_BRANDS.includes(normalizeBrand(raw));
}
