const { extract, requireAnchors, assertPure } = require('./dash_extract.js');
const { code, blocks } = extract();
console.log('script blocks:', blocks, '| code len:', code.length);
const groups = [
  ['const KEY_ID_LABELS = {', 'async function refreshInner(){', 'Key 列'],
  ['const BYKEY_RANGE = ', 'async function loadAnalytics(){', 'by_key 档位'],
];
let ok = 0, bad = 0;
for (const [a, b, label] of groups) {
  try {
    const r = requireAnchors(code, [a, b], label);
    const p = assertPure(r, label);
    console.log('  [PASS] ' + label + ' 锚点存在（region ' + r.length + ' 字符）| 纯区检查 ' + (p.ok ? 'OK' : 'FAIL ' + JSON.stringify(p.hits)));
    ok++;
  } catch (e) {
    console.log('  [MISS] ' + label + ' -> ' + e.message);
    bad++;
  }
}
console.log('selftest: ' + ok + ' ok, ' + bad + ' miss');
process.exit(0);
