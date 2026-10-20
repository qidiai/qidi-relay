/* 档位三态列的渲染 + Key 列四态 + 「按 Key 档位分布」的口径行。
 *
 * 为什么测：P1-3 的审计价值全靠这两列——「请求了但没生效」（实际下发为空串）是面板上
 * 唯一能看到「被丢弃」的地方，而「键缺失（客户端没请求）」与「空串（请求了没生效）」
 * 必须显示成两件事，否则运维会把「没请求」误读成「被上游吃掉」。
 * Key 列则必须只出**标识**：key_id 缺失=历史行/面板会话（不是「已删除」），
 * 真 12 位 hex 查不到才算已删除；后端桶名属于实现细节，不得出现在界面上（含 tooltip）。
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
const KEY_PART = slice('const KEY_ID_LABELS = {', 'async function refreshInner(){');
const BYKEY_CONSTS = slice('const BYKEY_RANGE = ', 'async function loadByKey(){');
const STATS = slice('function effortStatsByKey(rows){', 'function renderEffortByKey(){');
const RENDER = slice('function renderEffortByKey(){', 'async function loadAnalytics(){');
check('锚点：Key/档位列区、by-key 常量区、汇总函数、趋势面板渲染区都抽到',
  KEY_PART.length > 400 && BYKEY_CONSTS.length > 60 && STATS.length > 200 && RENDER.length > 400);
check('纯函数区（effortStatsByKey）不引用 document/window', !/document\.|window\./.test(STATS));

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = n => (n ?? 0).toLocaleString('en-US');
const body = { _html: '' };
const doc = { getElementById: (id) => id === 'effortByKey'
  ? { set innerHTML(v){ body._html = v; }, get innerHTML(){ return body._html; } } : null };
const api = new Function('document', 'esc', 'fmt', 'window',
  KEY_PART + BYKEY_CONSTS + STATS + RENDER +
  '; return {keyCell:keyCell, keyDisplay:keyDisplay, effortCell:effortCell, renderEffortByKey:renderEffortByKey, setSnap:(s)=>{RECENT_SNAPSHOT=s;}};')(
  doc, esc, fmt, { API_KEY_ROWS: [{ id: 'abc123def456', name: 'DSH 本地测试' }] });
const strip = h => String(h).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

// —— 档位三态 ——
check('同值 → 绿底单值（原样透传）',
  /badge ok/.test(api.effortCell({ reasoning_effort_requested: 'medium', reasoning_effort: 'medium' })));
const shifted = api.effortCell({ reasoning_effort_requested: 'high', reasoning_effort: 'medium' });
check('不同 → 黄底「A → B」（被挪档）', /badge warn/.test(shifted) && /high/.test(shifted) && /→/.test(shifted) && /medium/.test(shifted));
const dropped = api.effortCell({ reasoning_effort_requested: 'xhigh', reasoning_effort: '' });
check('实际空串 → 红底「→ 未下发」（请求了但被丢弃）', /badge bad/.test(dropped) && /未下发/.test(dropped));
check('键缺失 → 「—」（与空串严格区分，不显示「未下发」）',
  /—/.test(api.effortCell({})) && !/未下发/.test(api.effortCell({})));
check('只有实际值 → 「服务端默认」', /服务端默认/.test(api.effortCell({ reasoning_effort: 'low' })));

// —— Key 列四态 ——
check('缺 key_id → 「历史行未记录 Key」', /历史行未记录 Key/.test(api.keyCell({})));
check('legacy → 「默认（面板切换）」', /默认（面板切换）/.test(api.keyCell({ key_id: 'legacy' })));
check('launcher → 「启动参数」', /启动参数/.test(api.keyCell({ key_id: 'launcher' })));
check('真 12 位 hex 且不在配置里 → 「已删除的 Key」', /已删除的 Key/.test(api.keyCell({ key_id: '0011223344ff' })));
const known = api.keyCell({ key_id: 'abc123def456' });
check('面板里的 Key → 备注名 + 短标识（不出明文）', /DSH 本地测试/.test(known) && /abc1…f456/.test(known));
check('Key 列任何分支（含 tooltip）都不出现原始桶名',
  !/\(no-key\)/.test(api.keyCell({}) + api.keyCell({ key_id: '(no-key)' })));

// —— 档位分布面板 ——
const rows = [
  { key_id: 'abc123def456', reasoning_effort_requested: 'medium', reasoning_effort: 'medium' },
  { key_id: 'abc123def456', reasoning_effort_requested: 'high', reasoning_effort: 'medium' },
  { key_id: 'abc123def456', reasoning_effort_requested: 'xhigh', reasoning_effort: '' },
  { key_id: 'abc123def456' },
  { key_id: 'legacy', reasoning_effort_requested: 'low', reasoning_effort: 'low' },
  {}, { key_id: '(no-key)', reasoning_effort_requested: 'high', reasoning_effort: 'high' }
];
api.setSnap({ page: 2, limit: 20, total: 76, rows: rows });
api.renderEffortByKey();
const txt = strip(body._html);
check('口径行写明「基于最近请求表当前页」+ 样本/全量（避免与上面矩阵数字对不上被误判为 bug）',
  /基于最近请求表当前页/.test(txt) && /样本 7 条/.test(txt) && /全量 76 条/.test(txt));
check('口径行明确「不是上方按 Key 矩阵的全量聚合」「数字不可相减」', /不是/.test(txt) && /不可相减/.test(txt));
check('无 Key 桶显示为「历史行未记录 Key」，原始桶名不泄露',
  /历史行未记录 Key/.test(txt) && !/\(no-key\)/.test(body._html));
check('被挪档 / 被丢弃 用彩色徽标给出计数',
  /class="badge warn"[^>]*>1</.test(body._html) && /class="badge bad"[^>]*>1</.test(body._html));
api.setSnap(null);
api.renderEffortByKey();
check('样本未就绪 → 友好占位而不是空白/报错', /等待最近请求数据/.test(strip(body._html)));

// —— 结构不变式 ——
const th = (RENDER.match(/<th[ >]/g) || []).length;
const td = (RENDER.match(/<td[ >]/g) || []).length;
check('档位分布表：表头列数 == 行内单元格列数（' + th + ' == ' + td + '）', th > 0 && th === td);

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
