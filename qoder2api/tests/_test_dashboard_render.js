/* 前端渲染套件（层 1 + 层 2）：读 dashboard.html → 抽 script 块 → 内联元素桩 → 断言。
 *
 * 做法照 wb 的 tests/_test_matrix_filters.js（工作包里提到的 _dom_stub.js **不存在**）：
 * 每个套件自读 dashboard.html、自抽 <script>、自己内联手写一份约 15 行的元素桩，
 * 断言跑的就是**发布的那份代码**，不是副本。
 *
 *   node tests/_test_dashboard_render.js
 */
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');

const _crypto = require('crypto');
const _self = fs.readFileSync(__filename);
console.log('SUITE: ' + path.basename(__filename) + ' md5=' + _crypto.createHash('md5').update(_self).digest('hex'));
const _dashRoot = process.env.QD_DASH_ROOT || path.join(__dirname, '..');
const _dashRaw = fs.readFileSync(path.join(_dashRoot, 'dashboard.html'));
console.log('FILE: dashboard.html md5=' + _crypto.createHash('md5').update(_dashRaw).digest('hex') + ' bytes=' + _dashRaw.length + ' as-of=' + new Date().toISOString());
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = blocks.join('\n');

// ---- 内联元素桩（约 15 行）----
const els = {};
const mk = (id) => (els[id] = els[id] || {
  id, innerHTML: '', textContent: '', value: '', className: '', disabled: false,
  classList: { add(){}, remove(){}, contains(){ return false; } },
  style: {}, children: [], dataset: {},
  appendChild(c){ this.children.push(c); }, insertAdjacentHTML(){},
  querySelectorAll(){ return []; }, querySelector(){ return null; },
  addEventListener(){}, setAttribute(){}, getAttribute(){ return ''; },
  focus(){}, blur(){}, click(){}, remove(){}, scrollTop: 0, scrollHeight: 0,
});
global.document = {
  getElementById: (id) => (id ? mk(id) : null),
  querySelectorAll: () => [], querySelector: () => null,
  createElement: (t) => mk('__new_' + t), addEventListener(){},
  body: mk('body'), documentElement: mk('html'),
};
const _ss = { getItem: () => null, setItem(){}, removeItem(){} };
global.window = { addEventListener(){}, location: { search: '', href: '' },
  matchMedia: () => ({ matches: false, addEventListener(){} }),
  sessionStorage: _ss, localStorage: _ss };
global.sessionStorage = _ss;
global.localStorage = _ss;
global.navigator = { userAgent: 'node' };
global.fetch = async () => ({ status: 200, ok: true, text: async () => '{}', json: async () => ({}) });

let PASS = 0, FAIL = 0;
const check = (label, ok, detail) => {
  if (ok) { PASS++; console.log("  [PASS] " + label); }
  else { FAIL++; console.log("  [FAIL] " + label + "  " + JSON.stringify(detail)); }
};

console.log("[dashboard-render] 前端渲染断言");
check("DOM 桩就绪：元素可读写 innerHTML",
  (() => { const el = document.getElementById('probe'); el.innerHTML = '<b>x</b>';
           return el.innerHTML === '<b>x</b>'; })());
check("dashboard.html 的 script 块可抽取", blocks.length >= 1 && code.length > 1000,
  [blocks.length, code.length]);

const s = code.indexOf("function fmtNextCheckin(x){");
const e = code.indexOf("async function doCheckin(btn){");
const pure = (s >= 0 && e > s) ? code.slice(s, e) : "";
let api = null;
if (pure) {
  try {
    api = new Function(pure + "\nreturn {fmtNextCheckin,checkinOutcome,checkinToastKind,checkinToastText};")();
  } catch (err) { api = null; }
}
check("能抽到渲染纯函数组（fmtNextCheckin / checkinOutcome / checkinToastKind / checkinToastText）",
  api !== null, pure ? pure.length : "anchor miss");
if (api) {
  check("fmtNextCheckin 优先原样返回 note",
    api.fmtNextCheckin({ next_available_note: '10-05 10:00（UTC+8）' }) === '10-05 10:00（UTC+8）');
  check("checkinOutcome：earned_credit>0 -> claimed",
    api.checkinOutcome({ uid: 'a', ok: true, earned_credit: 100 }).state === 'claimed');
  check("checkinOutcome：earned=0 且无 claimed -> idle（不报签到成功）",
    api.checkinOutcome({ uid: 'b', ok: true, earned_credit: 0, claimed: [] }).state === 'idle'
    && !/签到成功/.test(api.checkinOutcome({ uid: 'b', ok: true, earned_credit: 0, claimed: [] }).text));
  check("checkinOutcome：ok=false -> failed",
    api.checkinOutcome({ uid: 'c', ok: false, error: 'x' }).state === 'failed');
  check("checkinToastKind：有到账 -> ok（成功色只在真到账时）",
    api.checkinToastKind([api.checkinOutcome({ uid: 'd', ok: true, earned_credit: 1 })]) === 'ok');
  check("checkinToastKind：全 idle -> warn（不是成功色）",
    api.checkinToastKind([api.checkinOutcome({ uid: 'e', ok: true, claimed: [] })]) === 'warn');
}
// 日志区必须是追加式（不整体重建），否则每次刷新会丢用户滚动位置/搜索态
check("日志区用追加式写法（insertAdjacentHTML 或 += 拼接）",
  /insertAdjacentHTML\(/.test(code) || /\+= *'<div/.test(code));

console.log("");
console.log("SUMMARY: TOTAL " + (PASS + FAIL) + " checks, " + PASS + " passed, " + FAIL + " failed");
console.log("RESULT: " + (FAIL ? "RED" : "GREEN") + " (exit " + (FAIL ? 1 : 0) + ")");
process.exit(FAIL ? 1 : 0);
