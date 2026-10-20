/* ① 日志区必须「增量追加」而不是每次轮询整体重建 ② 账号护栏四态徽标。
 *
 * 为什么测日志：日志每 2 秒轮询一次、DOM 上限 2000 行；如果每次都重建 2000 行，
 * 界面会卡顿并丢掉用户的滚动位置与上翻阅读状态（这是一个已修过的性能缺陷）。
 * 这把「追加而非重建」用**行为断言**钉死（静态 grep 是廉价版，这里是强化版）。
 * 为什么测四态：阈值 0 / 计数未知 / 已超额 / 正常必须能区分——把「开了但计数未知」
 * 显示成「没开」，运维会以为护栏没生效而重复配置。
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
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = n => (n ?? 0).toLocaleString('en-US');

// ---------------- ② 护栏四态徽标 ----------------
const BADGE = slice('function accountGuardBadge(a){', 'function accountRow(a){');
check('锚点：护栏徽标区抽到', BADGE.length > 400);
const badgeApi = new Function('fmt', 'esc', BADGE + '; return accountGuardBadge;')(fmt, esc);
const strip = h => String(h).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const titleOf = h => (h.match(/title="([^"]*)"/) || [])[1] || '';
check('阈值全 0 → 「未启用」', /未启用/.test(strip(badgeApi({ reserveCredits: 0, dailyTokenLimit: 0, dailyCreditLimit: 0 }))));
const unknown = badgeApi({ reserveCredits: 500, reserveBlocked: false, credits: null });
check('阈值>0 但计数未知（fail-open 放行中）→ 「未知」且 title 说明放行中',
  /未知/.test(strip(unknown)) && /计数未知（放行中）/.test(titleOf(unknown)));
const blocked = badgeApi({ reserveCredits: 500, reserveBlocked: true, credits: { remain: 120, size: 3000 } });
check('守卫命中 → 「已暂停」且 title 带阈值/余额', /已暂停/.test(strip(blocked)) && /阈值 500/.test(titleOf(blocked)));
check('计数已知未命中 → 「正常」', /正常/.test(strip(badgeApi({ dailyTokenLimit: 5000000, dailyTokensToday: 1200, dailyLimitBlocked: false }))));
check('单模型受限也计入「已暂停」', /已暂停/.test(strip(badgeApi({ modelTokenBlocked: ['qwen3-max'] }))));

// ---------------- ① 日志增量追加 ----------------
const LOG = slice('let logEntries = [];', 'function copyFilteredLogs(){');
check('锚点：日志模块区抽到', LOG.length > 1500);
function makeLogEnv(){
  const stats = { innerWrites: 0, inserts: 0, removes: 0 };
  const PH = { cls: 'log-empty', parentNode: null, offsetHeight: 24 };
  const body = {
    lines: [], empty: false, scrollTop: 0, clientHeight: 600,
    get scrollHeight(){ return Math.max(600, this.lines.length * 24); },
    get childElementCount(){ return this.lines.length + (this.empty ? 1 : 0); },
    get firstElementChild(){ return this.lines.length ? this.lines[0] : (this.empty ? PH : null); },
    querySelector(sel){
      if(sel === '.log-empty') return this.empty ? PH : null;
      if(sel === '.log-line') return this.lines[0] || null;
      return null;
    },
    _append(htmlStr){
      const msgs = [...htmlStr.matchAll(/class="log-msg">([^<]*)</g)].map(m => m[1]);
      msgs.forEach(m => this.lines.push({ msg: m, offsetHeight: 24 }));
      return msgs.length;
    },
    insertAdjacentHTML(pos, htmlStr){ stats.inserts++; this._append(htmlStr); },
    removeChild(node){ stats.removes++; if(node === PH){ this.empty = false; return; }
      const i = this.lines.indexOf(node); if(i >= 0) this.lines.splice(i, 1); },
    set innerHTML(v){ stats.innerWrites++; this.lines = []; this.empty = /log-empty/.test(v); if(!this.empty) this._append(v); },
    get innerHTML(){ return ''; }
  };
  PH.parentNode = body;
  const api = new Function('document', 'setTimeout', 'setInterval', 'clearInterval',
    LOG + '; return {renderLogs:renderLogs, push:(a)=>{a.forEach(x=>logEntries.push(x));}, clear:()=>{logEntries=[]; lastLogId=0;},' +
    ' filter:(o)=>{logFilterLevel=o.level||""; logFilterTag=o.tag||""; logSearchText=o.search||"";}};')(
    { getElementById: (id) => id === 'logTerminalBody' ? body : null }, setTimeout, setInterval, clearInterval);
  return { body: body, stats: stats, api: api };
}
const E = (i, level) => ({ id: i, time: 't' + i, level: level || 'INFO', tag: 'chat', msg: 'm' + i });
let env = makeLogEnv();
env.api.push([E(1), E(2), E(3)]); env.api.renderLogs(true);
check('首批加载 → 一次全量渲染，3 行', env.body.lines.length === 3 && env.stats.innerWrites === 1);
env.api.push([E(4), E(5)]); env.api.renderLogs(false);
check('* 轮询追加 → 只 append、**不重建**（innerHTML 写入次数不增加）',
  env.body.lines.length === 5 && env.stats.innerWrites === 1 && env.stats.inserts === 2);
env.api.renderLogs(false);
check('无新数据时重渲染是幂等的（不空转、不重复插入）', env.body.lines.length === 5 && env.stats.inserts === 2);
env = makeLogEnv();
const many = []; for(let i = 1; i <= 2000; i++) many.push(E(i));
env.api.push(many); env.api.renderLogs(true);
const more = []; for(let i = 2001; i <= 2100; i++) more.push(E(i));
env.api.push(more); env.api.renderLogs(false);
check('超过上限时从头部裁剪，DOM 行数恒 ≤ 2000 且首行后移（保留最新）',
  env.body.lines.length === 2000 && env.body.lines[0].msg === 'm101'
  && env.body.lines[env.body.lines.length - 1].msg === 'm2100' && env.stats.innerWrites === 1);
env.api.filter({ level: 'INFO' }); env.api.renderLogs();
check('过滤条件变化 → 触发一次重建（而不是在旧 DOM 上追加）',
  env.stats.innerWrites === 2 && env.body.lines.length === 2000);
env.api.filter({ level: 'ERROR' }); env.api.renderLogs();
check('过滤后没有命中行 → 回到空态占位（不残留上一次的行）',
  env.body.lines.length === 0 && env.body.empty === true);
const beforeRestore = env.stats.innerWrites;
env.api.filter({}); env.api.renderLogs();
check('恢复无过滤 → 从缓冲区重建出全部行（恰好一次重建）',
  env.body.lines.length === 2000 && env.stats.innerWrites === beforeRestore + 1);
env.api.clear(); env.api.renderLogs();
check('清空日志 → 回到空态占位', env.body.lines.length === 0 && env.body.empty === true);

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
