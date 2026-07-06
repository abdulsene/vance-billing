// Pure-Node test for the enrollment card-brand guard. Exits non-zero on failure.
//   run: npm run test:brand   (or: node test/brand_guard.test.mjs)
import {
  isAcceptedBrand, normalizeBrand, ACCEPTED_BRANDS, UNSUPPORTED_CARD_MESSAGE,
} from '../enrollment/frontend/brand_guard.mjs';

let failures = 0;
function check(name, cond) {
  if (cond) { console.log('  ok   - ' + name); }
  else { console.error('  FAIL - ' + name); failures++; }
}

// Accepted: Visa + Mastercard (any casing / alias) pass.
for (const b of ['visa', 'Visa', 'VISA', 'mastercard', 'MasterCard', 'master card', 'mc']) {
  check('accepted: ' + b, isAcceptedBrand(b) === true);
}

// Blocked: Amex / Discover (and unknowns / empty) are rejected.
for (const b of ['amex', 'Amex', 'American Express', 'discover', 'Discover', 'jcb', 'diners', '', null, undefined]) {
  check('blocked: ' + String(b), isAcceptedBrand(b) === false);
}

check('ACCEPTED_BRANDS === visa,mastercard', ACCEPTED_BRANDS.join(',') === 'visa,mastercard');
check('normalizeBrand("MC") === mastercard', normalizeBrand('MC') === 'mastercard');
check('unsupported message names Visa and Mastercard', /Visa and Mastercard/.test(UNSUPPORTED_CARD_MESSAGE));

if (failures > 0) {
  console.error('\n' + failures + ' assertion(s) FAILED');
  process.exit(1);
}
console.log('\nAll brand_guard assertions passed');
