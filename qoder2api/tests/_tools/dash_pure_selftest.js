/* 纯函数体检：把 dashboard 的渲染函数分成「真纯」与「需注入 window」两类。
 *
 * 实测发现（值得留档）：`keyDisplay` **不是纯的** —— 它读 `window.API_KEY_ROWS`
 * （`typeof API_KEY_ROWS !== 'undefined' && API_KEY_ROWS.length` 那一行）。
 * 棉花糖给的注入列表里本来就有 window，所以她的方案是对的；这条体检的价值是：
 * 将来谁把 window 依赖扩散到更多函数（或反过来移除注入），这里会立刻红。
 */
const { extract, assertPureFn } = require('./dash_extract.js');
const { code } = extract();

const PURE = ['keyCell', 'effortCell', 'effortStatsByKey', 'checkinOutcome', 'fmtNextCheckin'];
const NEEDS_WINDOW = ['keyDisplay'];      // 读 window.API_KEY_ROWS，断言时必须注入

let fail = 0;
const p = assertPureFn(code, PURE, '真纯函数');
console.log('真纯函数（' + PURE.length + ' 个）：' + (p.ok ? 'PASS' : 'FAIL'));
if (!p.ok) { fail++; for (const b of p.bad) console.log('   ' + b.fn + ' -> ' + JSON.stringify(b.why)); }

const w = assertPureFn(code, NEEDS_WINDOW, '需注入 window');
const expectedImpure = !w.ok;
console.log('需注入 window（' + NEEDS_WINDOW.length + ' 个）：' +
            (expectedImpure ? 'PASS（确实引用了 window，与预期一致）' : 'FAIL（预期它引用 window，但没有）'));
if (!expectedImpure) fail++;

console.log('');
console.log(fail ? 'RESULT: RED' : 'RESULT: GREEN');
process.exit(fail ? 1 : 0);
