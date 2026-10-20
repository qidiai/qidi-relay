/* #26 自动路由：三态开关（自动/锁国内/锁国际）+ 最近一次对话的出口（**数据源＝usage 行**）+ 区域列。
 *
 * 为什么测：自动换区是**后台替用户做的决定**，隐式行为必须可见。
 * ⚠️ 数据源已按契约迁移：X-Qoder-Realm(-Reason) 只在 chat 类请求且自动换区时出现，管理端点永不带，
 *    所以面板**不再读任何响应头**，改读 usage 行的 realm / realm_reason（持久化、可查历史）。
 *    本套件因此把「响应头桩」全部换成了「usage 行夹具」，并加了两条**反向保险**（防止将来有人把已切断的
 *    数据源接回来）。三态严格区分：键不存在=不知道 / 空串=没发生（没换区） / 其余=换区+确定性原因。
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
const FN = slice('function realmModeOf(state){', 'function updateUI(){');
const CONSTS = slice('const BYKEY_RANGE = ', 'async function loadByKey(){');   // 提供 RECENT_SNAPSHOT 的声明
check('锚点：realmModeOf / realmActualNote / 渲染与切换函数都抽到（含 RECENT_SNAPSHOT 常量区）', FN.length > 1800 && CONSTS.length > 60);
// 片段依赖自检：eval 范围必须自带它引用的声明。少了这条，缺声明会以「ReferenceError 崩掉、没有 SUMMARY」的形式出现，
// 还会被误读成「dashboard.html 坏了」——实际上浏览器加载整块脚本时声明就在，只有按片段 eval 的套件才需要它。
check('片段依赖自检：eval 范围内含 RECENT_SNAPSHOT 的声明（否则套件会崩，而不是假绿）',
  /let RECENT_SNAPSHOT = null;/.test(CONSTS));
check('片段依赖自检：dashboard.html 里 RECENT_SNAPSHOT 是顶层唯一声明，且所有引用都在函数体内（浏览器加载即求值，无 TDZ 风险）',
  (html.match(/let RECENT_SNAPSHOT = null;/g) || []).length === 1
  && /function renderRealmActual[\s\S]{0,600}RECENT_SNAPSHOT/.test(html));
check('标记：三个档位按钮与「实际出口」行都在 DOM 里（自动 / 锁定国内 / 锁定国际）',
  /id="realmModeAuto"[^>]*>自动</.test(html) && /id="realmModeCn"[^>]*>锁定国内</.test(html)
  && /id="realmModeIntl"[^>]*>锁定国际</.test(html) && /id="realmActual"/.test(html));

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const els = {};
function mk(id){
  const el = { id: id, _cls: new Set(), title: '', innerHTML: '', textContent: '', style: {},
    classList: { add(c){ el._cls.add(c); }, remove(c){ el._cls.delete(c); }, contains(c){ return el._cls.has(c); },
                 toggle(c, on){ if(on === undefined) on = !el._cls.has(c); if(on) el._cls.add(c); else el._cls.delete(c); } } };
  return el;
}
const posted = []; const toasts = [];
function load(state, win){
  Object.keys(els).forEach(k => delete els[k]);
  const W = win || {};
  W.REALM_STATE = state;
  W.ACTIVE_GATEWAY_REALM = (state || {}).current || 'intl';
  W.updateUI = () => {}; W.VIEW_REALM = W.ACTIVE_GATEWAY_REALM;
  const doc = { getElementById: (id) => (els[id] = els[id] || mk(id)) };
  const api = new Function('document', 'esc', 'window', 'toast', 'postJSON', 'loadAccounts', 'loadModels', 'refresh',
    CONSTS + FN + '; return {realmModeOf:realmModeOf, realmActualNote:realmActualNote, renderRealmMode:renderRealmMode,' +
    ' renderRealmActual:renderRealmActual, setRealmMode:setRealmMode,' +
    ' setSnap:(rows)=>{RECENT_SNAPSHOT={page:1,limit:20,total:rows.length,rows:rows};}};')(
    doc, esc, W,
    (m, k) => toasts.push(k + ':' + m),
    async (url, body) => { posted.push({ url: url, body: body }); return { current: (body || {}).realm }; },
    async () => {}, async () => {}, async () => {});
  return { api: api, els: els, W: W };
}
const rowOf = (o) => Object.assign({ realm: 'intl', iso: '2025-10-05T12:03:07' }, o);

// —— 三态判定 ——
let env = load({ current: 'intl', auto_enabled: true, auto_env: 'QD_AUTO_REALM' });
check('auto_enabled=true → 当前档位是「自动」', env.api.realmModeOf(env.W.REALM_STATE) === 'auto');
env = load({ current: 'cn', auto_enabled: false });
check('锁定国内 → 「锁定国内」', env.api.realmModeOf(env.W.REALM_STATE) === 'cn');
env = load({ current: 'intl', auto_enabled: false });
check('锁定国际 → 「锁定国际」', env.api.realmModeOf(env.W.REALM_STATE) === 'intl');
env.api.renderRealmMode();
const actives = ['realmModeAuto', 'realmModeCn', 'realmModeIntl'].filter(id => env.els[id] && env.els[id].classList.contains('active'));
check('渲染：三档里**恰好一个**处于 active（不会出现两档同时亮）', actives.length === 1 && actives[0] === 'realmModeIntl');
check('渲染：悬停提示写明环境变量名（改它要重启，面板只展示）', /QD_AUTO_REALM/.test(env.els['realmModeBar'].title));

// —— 最近一次对话的出口（usage 行；三态 + 时间 + 样本范围） ——
env = load({ current: 'intl', auto_enabled: true });
let note = env.api.realmActualNote([], env.W.REALM_STATE);
check('没有 usage 行 → 「还不确定」且**不显示默认值**（拿不到就不装作知道）',
  /还不确定/.test(note.head) && /不显示默认值/.test(note.detail));
note = env.api.realmActualNote([rowOf({ realm_reason: '' })], env.W.REALM_STATE);
check('realm_reason 空串 → 「未换区」（没发生），head 带时间戳',
  /未换区/.test(note.head) && /10-05 12:03:07/.test(note.head) && note.tone === 'dim');
check('空串 + 整页都没换过 → 明说「本页 N 条样本里没有出现过自动换区」（不吹成「从没换过」）',
  /本页 1 条样本里没有出现过自动换区/.test(note.detail));
note = env.api.realmActualNote([rowOf({ realm: 'intl', realm_reason: 'model_missing' })], env.W.REALM_STATE);
check('realm_reason=model_missing → 「被自动换区」+ 确定性原因 + 行动含义 + 时间',
  /被自动换区/.test(note.head) && /加账号也改变不了/.test(note.detail) && /10-05 12:03:07/.test(note.head) && note.tone === 'warn');
note = env.api.realmActualNote([rowOf({ realm: 'cn', realm_reason: 'no_account' })], env.W.REALM_STATE);
check('realm_reason=no_account → 原因 + 「加一个可用账号即可」', /加一个可用账号即可/.test(note.detail) && /国内版/.test(note.head));
note = env.api.realmActualNote([rowOf({})], env.W.REALM_STATE);
check('**键不存在（字段上线前的旧行）→ 「未知（旧记录）」，不猜换没换**（与「没换区」严格区分）',
  /未知（旧记录）/.test(note.head) && /该字段上线之前/.test(note.detail) && !/未换区/.test(note.head));
note = env.api.realmActualNote([rowOf({ realm_reason: '' }), rowOf({ realm_reason: 'no_account', iso: '2025-10-05T09:00:00' })], env.W.REALM_STATE);
check('最近一条没换但样本里有换过 → 明说「本页样本里有 1 条被自动换过区，最近这一条没有」',
  /有 1 条被自动换过区，最近这一条没有/.test(note.detail));
env = load({ current: 'cn', auto_enabled: true });
env.api.renderRealmActual();   // RECENT_SNAPSHOT 初始为 null
check('渲染：拿不到行时显示「还不确定」而不是空白/默认区',
  /还不确定/.test(env.els['realmActual'].innerHTML) && env.els['realmActual'].style.color === 'var(--dim)');
env.api.setSnap([rowOf({ realm: 'intl', realm_reason: 'no_account' })]);
env.api.renderRealmActual();
check('渲染：换区时配色变 warn，且把「时间 + 区域 + 被自动换区」写全',
  env.els['realmActual'].style.color === 'var(--warn)' && /被自动换区/.test(env.els['realmActual'].innerHTML)
  && /10-05 12:03:07/.test(env.els['realmActual'].innerHTML) && /国际版/.test(env.els['realmActual'].innerHTML));
note = env.api.realmActualNote([Object.assign(rowOf({ error: true, realm_reason: 'no_account' }), {}), rowOf({ realm_reason: '' })], env.W.REALM_STATE);
check('最新一条是 error 行 → 跳过它取最近一条非 error 行（失败行不该当结论）', /未换区/.test(note.head));

// —— 反向保险：已切断的数据源不得被接回来 ——
// 反向保险：**检查对象是 dashboard.html 全文**（不是本套件自己的源码——查自己永远是绿的），
// 且覆盖两个历史读取点（原 :1911 的 realm 读取、:1916 的 reason 读取）。
const headerReads = html.match(/headers\.get\(\s*['"]X-Qoder-Realm[^'"]*['"]/g) || [];
check('反向保险：dashboard.html 全文不得再读 X-Qoder-Realm(-Reason) 响应头（历史读取点 2 处，现应为 0）',
  headerReads.length === 0);
check('反向保险：遗留的 LAST_REALM_SEEN 变量已彻底移除（没有半死不活的旧路径）', !/LAST_REALM_SEEN/.test(html));
check('反向保险自证材料：检查对象确认是 dashboard.html（含 <script> 块，非空）',
  html.length > 100000 && html.includes('<script>'));

// —— 最近请求表的「区域」列（usage 行逐请求落地的确定值） ——
const RB = slice('function realmBadgeOf(realm){', 'function keyCell(r){');
check('锚点：区域徽标函数抽到', RB.length > 120);
const rbadge = new Function(RB + '; return realmBadgeOf;')();
check('intl 行 → 「国际版」+ intl 样式类', /国际版/.test(rbadge('intl')) && /realm-badge-intl/.test(rbadge('intl')));
check('cn 行 → 「国内版」+ cn 样式类', /国内版/.test(rbadge('cn')) && /realm-badge-cn/.test(rbadge('cn')));
check('缺 realm（旧行）→ 「—」且不猜区域', /—/.test(rbadge('')) && /—/.test(rbadge(undefined)));
check('表头有「区域」列且排在档位之后（与审计两列相邻）', /档位<\/th><th style="text-align:center;width:72px">区域<\/th>/.test(html));
check('最近请求表：表头列数 == 行内单元格列数（加了区域列后仍然对齐）', (() => {
  const lines = html.split('\n');
  const hIdx = lines.findIndex(l => l.includes('<th>时间</th>'));
  const eIdx = lines.findIndex((l, i) => i > hIdx && l.includes('</tbody></table>'));
  const blk = lines.slice(hIdx, eIdx + 1).join('\n');
  const th = (blk.match(/<th[ >]/g) || []).length, td = (blk.match(/\+ '<td/g) || []).length;
  return th > 0 && th === td;
})());
check('图例写明「卡片那条与区域列**同源**（都读 usage 行）」（两处不再互相冒充）',
  /同源/.test(html) && !/那条来自响应头/.test(html));

// —— 开关行为 ——
env = load({ current: 'cn', auto_enabled: true, auto_env: 'QD_AUTO_REALM' });
posted.length = 0; toasts.length = 0;
env.api.setRealmMode('auto');
check('点「自动」→ **不发请求**（环境变量只能重启改），只给提示', posted.length === 0 && /环境变量/.test(toasts[0] || ''));
posted.length = 0;
env.api.setRealmMode('cn');
check('点「锁定国内」→ POST /realm {realm:"cn"}', posted.length === 1 && posted[0].url === '/realm' && posted[0].body.realm === 'cn');

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
