/* 档位「逐行审计列」与「按 Key 汇总视图」的口径一致性（最高优先）。
 *
 * 为什么要测：同一份数据在两处实现——最近请求表的档位列（effortCell，逐行）与
 * 「各 Key 的档位分布」（effortStatsByKey，汇总）。两套口径一旦漂移，运维会同时看到
 * 互相矛盾的数字而不知道该信哪个（同类教训：sum(by_key)==sum(by_account) 的同源要求）。
 * 这条断言把「两处必须一致」钉死：换任何一侧的判定都会立刻变红。
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
const NO_KEY = slice("const KEY_NO_KEY_ID = ", 'function keyDisplay(id){');
const EFFORT_CELL = slice('function effortCell(r){', 'async function refreshInner(){');
const STATS = slice('function effortStatsByKey(rows){', 'function renderEffortByKey(){');

check('锚点：三处纯函数区都抽到了（锚点漂移会先在这里红，而不是静默抽到空串）',
  NO_KEY.length > 20 && EFFORT_CELL.length > 100 && STATS.length > 200);
check('纯函数区不引用 document/window（防止将来把渲染塞进来、让断言变成依赖桩的假绿）',
  !/document\.|window\./.test(EFFORT_CELL + STATS));

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const api = new Function('esc', NO_KEY + EFFORT_CELL + STATS + '; return {effortCell:effortCell, effortStatsByKey:effortStatsByKey};')(esc);

// 夹具：一条 Key 覆盖四种形态 + legacy + 无 Key（键缺失 ≠ 空串）
const ROWS = [
  { key_id: 'abc123def456', reasoning_effort_requested: 'medium', reasoning_effort: 'medium' },  // 透传
  { key_id: 'abc123def456', reasoning_effort_requested: 'high',   reasoning_effort: 'medium' },  // 挪档
  { key_id: 'abc123def456', reasoning_effort_requested: 'xhigh',  reasoning_effort: '' },        // 丢弃
  { key_id: 'abc123def456' },                                                                    // 未请求（键全缺）
  { key_id: 'legacy',       reasoning_effort_requested: 'low',    reasoning_effort: 'low' },
  {},
  { key_id: '(no-key)',     reasoning_effort_requested: 'high',   reasoning_effort: 'high' }
];
const stats = api.effortStatsByKey(ROWS);
const byId = {}; stats.forEach(s => { byId[s.key_id] = s; });

check('分组与排序：按样本数降序（4 / 2 / 1）', stats.map(s => s.total).join(',') === '4,2,1');
check('单 Key 四形态各归其类（1 透传 / 1 挪档 / 1 丢弃 / 1 未请求）',
  byId['abc123def456'].same === 1 && byId['abc123def456'].shifted === 1
  && byId['abc123def456'].dropped === 1 && byId['abc123def456'].none === 1);
check('缺 key_id 的行归入 (no-key) 桶，不丢弃',
  !!byId['(no-key)'] && byId['(no-key)'].total === 2 && byId['(no-key)'].same === 1);
check('legacy 单独成桶', !!byId['legacy'] && byId['legacy'].same === 1);

// —— 核心：逐行审计列 vs 汇总视图，四类计数必须一致 ——
const tally = { same: 0, shifted: 0, dropped: 0, none: 0 };
ROWS.forEach(r => {
  const h = api.effortCell(r);
  const k = /未下发/.test(h) ? 'dropped'
          : (/badge ok/.test(h) ? 'same' : (/badge warn/.test(h) ? 'shifted' : 'none'));
  tally[k]++;
});
const sum = stats.reduce((a, s) => ({ same: a.same + s.same, shifted: a.shifted + s.shifted,
                                      dropped: a.dropped + s.dropped, none: a.none + s.none }),
                         { same: 0, shifted: 0, dropped: 0, none: 0 });
check('* 逐行 effortCell 与汇总 effortStatsByKey 的四类计数完全一致',
  ['same','shifted','dropped','none'].every(k => tally[k] === sum[k]));
check('* 一致的具体数值符合设计意图（same=3 / shifted=1 / dropped=1 / none=2）',
  tally.same === 3 && tally.shifted === 1 && tally.dropped === 1 && tally.none === 2);
check('逐行渲染：同值=绿底色单值、不同=「A → B」、空串=「→ 未下发」、键缺失=「—」',
  /badge ok/.test(api.effortCell(ROWS[0])) && /→/.test(api.effortCell(ROWS[1]))
  && /未下发/.test(api.effortCell(ROWS[2])) && /^<span[^>]*>—<\/span>$/.test(api.effortCell(ROWS[3]).trim()));

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
