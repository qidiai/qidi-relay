/* #25 账本「未知」≠ 0 的三态渲染（福利中心额度卡片）。
 *
 * 为什么测：退化响应（全 0 + exceeded）不再覆盖健康快照、而是标记为「未知」；
 * 如果面板把它渲染成 0，用户会以为额度耗尽——这正是 issue #25 报告者被误导的地方。
 * 三态必须互相可分：正常数字 / 未知（含原因）/ 真 0（耗尽）；合计视图还必须点名
 * 「已排除了几个未知账号」，否则用户拿合计去和账号卡片对账会以为少算。
 */
const fs = require('fs');
const path = require('path');
const ROOT = process.env.QD_DASH_ROOT || path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'dashboard.html'), 'utf8');
// 套件自身指纹在**启动时**取一次：它代表**真正被执行的那份字节**（运行中被编辑也不会让这行说谎）
const SUITE_MD5 = require('crypto').createHash('md5').update(require('fs').readFileSync(__filename)).digest('hex');
let PASS = 0, FAIL = 0;
function check(name, cond){ if(cond){ PASS++; console.log('  ok   ' + name); } else { FAIL++; console.log('  FAIL ' + name); } }
function slice(from, to){
  const a = html.indexOf(from), b = html.indexOf(to);
  return (a >= 0 && b > a) ? html.slice(a, b) : '';
}
const FN = slice('function renderGrowthTasks(r){', 'async function runGrowthTasks(btn){');
check('锚点：renderGrowthTasks 抽到', FN.length > 600);
check('回归防护：不再存在「无条件 sum.energy || 0 → 直接当数字」的老写法',
  !/const energy = sum\.energy \|\| 0;/.test(html) && /const energyUnknown = /.test(FN));

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
// 桩要照 DOM 语义做：textContent 的 setter 会把数字转成字符串（否则 1234 vs '1234' 会误红）
function mk(){
  const el = { title: '', value: '', style: {}, className: '', _text: '', _html: '',
    classList: { add(){}, remove(){}, contains(){ return false; } }, appendChild(){}, addEventListener(){} };
  Object.defineProperty(el, 'textContent', { get(){ return el._text; }, set(v){ el._text = (v === null || v === undefined) ? '' : String(v); } });
  Object.defineProperty(el, 'innerHTML', { get(){ return el._html; }, set(v){ el._html = String(v); } });
  return el;
}
function run(sum, tasks){
  const els = {};
  const doc = { getElementById: (id) => (els[id] = els[id] || mk()), querySelector: () => null,
                querySelectorAll: () => [], createElement: () => mk() };
  // renderCodePanel 与本套件无关，用空桩隔离被测单元（它只负责兑换码面板）
  const api = new Function('document', 'esc', 'renderCodePanel', FN + '; return renderGrowthTasks;')(doc, esc, () => {});
  api({ tasks: tasks || [], summary: sum, accounts: [], mode: sum.mode || 'all' });
  return els;
}
// ① 正常数字
let e = run({ mode: 'all', energy: 1234, accounts_total: 3 });
check('正常数字 → 直接显示数字，标签为「合计积分余额（全部账号）」',
  e.nrgBalance.textContent === '1234' && /全部账号/.test(e.nrgLabel.textContent) && !/未知/.test(e.nrgLabel.textContent));
// ② 真 0（耗尽）
e = run({ mode: 'all', energy: 0, accounts_total: 3 });
check('真 0（耗尽）→ 显示 0，**不得**显示「未知」',
  e.nrgBalance.textContent === '0' && !/未知/.test(e.nrgBalance.innerHTML) && !/未知/.test(e.nrgLabel.textContent));
// ③ 未知（单账号）
const NOTE = '余额未知（上次刷新返回退化响应，等待下次成功刷新）';
e = run({ mode: 'uid', energy: null, credits_unknown: true, energy_note: NOTE });
check('未知 → 显示「未知」而不是 0，且带原因是 warn 色',
  /未知/.test(e.nrgBalance.innerHTML) && !/>0</.test(e.nrgBalance.innerHTML) && /var\(--warn\)/.test(e.nrgBalance.innerHTML));
check('未知 → 原因写进 title（energy_note）', new RegExp(NOTE).test(e.nrgBalance.innerHTML) || e.nrgBalance.title === NOTE);
check('未知 → 标签变「积分额度余额（未知）」', /（未知）/.test(e.nrgLabel.textContent));
// ④ 合计排除未知账号
e = run({ mode: 'all', energy: 500, accounts_total: 4, credits_unknown_accounts: ['aaaa1111bbbb', 'cccc2222dddd'] });
check('合计视图 → 标签点名「已排除 2 个未知账号」', /已排除 2 个未知账号/.test(e.nrgLabel.textContent));
check('合计视图 → title 列出未知账号（短标识）', /aaaa1111/.test(e.nrgLabel.title) && /cccc2222/.test(e.nrgLabel.title));
check('合计视图 → 数值仍显示已知合计（500）', e.nrgBalance.textContent === '500');
// ⑤ 边界
e = run({ mode: 'all', energy: 7 });
check('存在未知账号时不再声称「全部账号」', !/全部账号/.test(run({ mode: 'all', energy: 5, credits_unknown_accounts: ['x'] }).nrgLabel.textContent));
e = run({ mode: 'uid', energy: 0 });
check('旧后端兼容：缺 credits_unknown 且 non-null → 走数字路径（不误判未知）',
  e.nrgBalance.textContent === '0' && !/未知/.test(e.nrgLabel.textContent));

// 指纹两行：**被测文件**与**套件自身**，各带 as-of（UTC）——并发编辑下先比时间戳，再谈要不要重跑
console.log('SUITE: ' + path.basename(__filename) + ' md5=' + SUITE_MD5);
console.log('FILE: ' + path.basename(path.join(ROOT, 'dashboard.html'))
  + ' md5=' + require('crypto').createHash('md5').update(html).digest('hex')
  + ' bytes=' + Buffer.byteLength(html, 'utf8') + ' codeunits=' + html.length
  + ' as-of=' + new Date().toISOString());
console.log('');
console.log('SUMMARY: TOTAL ' + (PASS + FAIL) + ' checks, ' + PASS + ' passed, ' + FAIL + ' failed');
console.log('RESULT: ' + (FAIL ? 'RED' : 'GREEN') + ' (exit ' + (FAIL ? 1 : 0) + ')');
process.exit(FAIL ? 1 : 0);
