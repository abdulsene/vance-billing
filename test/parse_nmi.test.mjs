// Pure-Node test for the gate's NMI response parser. Exits non-zero on failure.
//   run: npm run test:gate   (or: node test/parse_nmi.test.mjs)
import { parseNmi } from './parse_nmi.mjs';

let failures = 0;
function check(name, cond) {
  if (cond) { console.log('  ok   - ' + name); }
  else { console.error('  FAIL - ' + name); failures++; }
}

// 1. Approved: response==="1" and all fields parsed.
{
  const r = parseNmi("response=1&responsetext=Approved&authcode=A1&transactionid=123&avsresponse=Y&cvvresponse=M&response_code=100");
  check('approved: response === "1"', r.response === '1');
  check('approved: responsetext', r.responsetext === 'Approved');
  check('approved: transactionid', r.transactionid === '123');
  check('approved: authcode', r.authcode === 'A1');
  check('approved: avsresponse', r.avsresponse === 'Y');
  check('approved: cvvresponse', r.cvvresponse === 'M');
  check('approved: response_code', r.response_code === '100');
}

// 2. "+"-encoded value decodes to spaces.
{
  const r = parseNmi("response=2&responsetext=Card+was+declined+by+issuer");
  check('plus decodes to spaces', r.responsetext === 'Card was declined by issuer');
}

// 3. Decline: response==="2".
{
  const r = parseNmi("response=2&responsetext=DECLINE");
  check('decline: response === "2"', r.response === '2');
  check('decline: responsetext', r.responsetext === 'DECLINE');
}

// 4. Empty / garbage / missing -> response undefined-or-empty (guard routes to dunning).
{
  const empty = parseNmi("");
  check('empty -> response undefined/empty', empty.response === undefined || empty.response === '');

  const garbage = parseNmi("totally not a valid nmi body");
  check('garbage -> response undefined/empty', garbage.response === undefined || garbage.response === '');

  const nothing = parseNmi(undefined);
  check('undefined raw -> response undefined/empty', nothing.response === undefined || nothing.response === '');
}

if (failures > 0) {
  console.error('\n' + failures + ' assertion(s) FAILED');
  process.exit(1);
}
console.log('\nAll parse_nmi assertions passed');
