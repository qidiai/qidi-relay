/* ① 403/401 分流（真 bug 的回归网）② 限额保存的请求形状。
 *
 * 为什么测 403/401：曾经前端把 403 与 401 一起当作「鉴权失效」处理——面板仍在默认密码时
 * 读明文 Key 会被后端 403 拒绝（这是正确的安全守卫），但前端却弹出登录遮罩，用户看到的是
 * 「点一下复制就掉登录」，而且真正的拒绝原因被吞掉。现在 401 才弹遮罩，403 保留会话并把
 * 服务端原因抛给调用处 toast。这条断了就等于那个 bug 回来了。
 * 为什么测限额 payload：留空=继承、显式 0=关闭 的语义必须在请求体里**原样**传递；
 * 一旦序列化走样（例如把继承写成 0），一次保存就会把区域阈值全部覆盖成「关闭」。
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

// ---------------- ① 403 / 401 ----------------
const AUTH = slice('async function throwPanelAuthError', 'function toast(msg, kind){');
check('锚点：认证包装区抽到', AUTH.length > 400);
check('三个包装函数都接了同一个 helper（getJSON / postJSON / downloadExport）',
  (html.match(/await throwPanelAuthError\(/g) || []).length === 3);
check('全文不再有把 401 与 403 合并判定的写法',
  !/status === 401 \|\| r\.status === 403|r\.status === 401 \|\| r\.status === 403/.test(html));

// 桩 Response：只实现 status/ok/json/text，第二次读抛哨兵 —— 证明没人二次消费 body
function fakeRes(status, body, jsonable){
  let reads = 0;
  return { status, ok: status >= 200 && status < 300, reads: () => reads,
    json(){ reads++; if(reads > 1) throw new Error('BODY-READ-TWICE'); if(jsonable === false) return Promise.reject(new Error('not json')); return Promise.resolve(body); },
    text(){ reads++; if(reads > 1) throw new Error('BODY-READ-TWICE'); return Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)); } };
}
let masked = 0, next = null;
const authApi = new Function('fetch', 'authHeaders', 'panelNeedsLogin',
  AUTH + '; return {getJSON:getJSON, postJSON:postJSON};')(
  () => Promise.resolve(next), () => ({}), () => { masked++; });

(async () => {
  const SERVER_MSG = '面板仍在使用默认密码，局域网内任何人都能登录后读到明文 API Key。';
  async function call(kind, res, args){
    next = res; masked = 0;
    try { await authApi[kind].apply(null, args); return { masked: masked, err: null, reads: res.reads() }; }
    catch(e) { return { masked: masked, err: e.message, reads: res.reads() }; }
  }
  let r = await call('getJSON', fakeRes(401, {}), ['/settings/reveal?id=k1']);
  check('401 → 弹登录遮罩、抛 unauthorized、且不读 body', r.masked === 1 && r.err === 'unauthorized' && r.reads === 0);
  r = await call('getJSON', fakeRes(403, { error: { message: SERVER_MSG } }), ['/settings/reveal?id=k1']);
  check('403 → 不弹遮罩（用户不掉登录）', r.masked === 0);
  check('403 → 抛出服务端原文（复制失败时 toast 能显示可操作原因）', /默认密码/.test(r.err || ''));
  check('403 → body 只被读一次（无二次消费）', r.reads === 1);
  r = await call('getJSON', fakeRes(403, 'html error page', false), ['/x']);
  check('403 且 body 不是 JSON → 兜底文案，不抛解析异常', /服务端拒绝了该操作（403）/.test(r.err || ''));
  r = await call('postJSON', fakeRes(403, { error: { message: SERVER_MSG } }), ['/settings/save', {}]);
  check('postJSON 403 → 复用已解析 body（text() 之后不再 json()）', r.masked === 0 && /默认密码/.test(r.err || '') && r.reads === 1);
  r = await call('postJSON', fakeRes(401, {}), ['/panel/logout', {}]);
  check('postJSON 401 → 仍然弹遮罩', r.masked === 1 && r.err === 'unauthorized');
  r = await call('getJSON', fakeRes(500, {}), ['/settings']);
  check('500 → 走普通错误路径，不弹遮罩', r.masked === 0 && r.err === '500');

  // ---------------- ② 限额保存载荷 ----------------
  const LIM = slice('const LIMIT_FIELDS = [', 'function switchMainTab(tab){');
  check('锚点：限额区抽到', LIM.length > 800);
  function makeEnv(){
    const st = { tbody: '', stateEl: { textContent: '', style: {} }, btn: { disabled: false, title: '', textContent: '' },
                 inputs: {}, posted: [], toasts: [] };
    const doc = {
      getElementById(id){
        if(id === 'limitsTbody') return { set innerHTML(v){ st.tbody = v; }, get innerHTML(){ return st.tbody; } };
        if(id === 'setLimitsState') return st.stateEl;
        if(id === 'btnSaveLimits') return st.btn;
        if(/^lim-(global|intl|cn)-/.test(id)) return st.inputs[id] || (st.inputs[id] = { value: '', placeholder: '' });
        return null;
      },
      querySelector(){ return null; }
    };
    const api = new Function('document', 'postJSON', 'toast', LIM + '; return {renderLimits:renderLimits, saveLimits:saveLimits};')(
      doc,
      async (url, body) => { st.posted.push({ url: url, body: body }); return { ok: true, limits: body.limits }; },
      (msg, kind) => st.toasts.push(kind + ':' + msg));
    return { st: st, api: api };
  }
  const MAP = {
    reserve_credits: { global: 500, intl: null, cn: 0 },
    daily_token_limit: { global: 0, intl: null, cn: null },
    daily_credit_limit: { global: 800, intl: 1200, cn: null },
    model_daily_token_limit: { global: 0, intl: null, cn: null },
    expiring_window_days: { global: 7, intl: null, cn: null }
  };
  let env = makeEnv();
  env.api.renderLimits(MAP);
  check('留空=继承：国际版显示「继承全局 500 积分点数」而不是 0',
    /继承全局 500 积分点数/.test(env.st.tbody) && /id="lim-intl-reserve_credits"[^>]*placeholder="继承全局 500"/.test(env.st.tbody));
  check('显式 0=关闭：国内版显示「关闭（本区域不启用）」', /关闭（本区域不启用）/.test(env.st.tbody));
  check('区域覆盖存在时状态标记为「含区域覆盖」且允许保存', env.st.stateEl.textContent === '(含区域覆盖)' && env.st.btn.disabled === false);
  env.st.inputs['lim-global-reserve_credits'].value = '500';
  env.st.inputs['lim-intl-reserve_credits'].value = '';
  env.st.inputs['lim-cn-reserve_credits'].value = '0';
  env.st.inputs['lim-global-daily_token_limit'].value = '10000000';
  await env.api.saveLimits(env.st.btn);
  const sent = env.st.posted[0];
  check('保存打到 /settings/save', !!sent && sent.url === '/settings/save');
  check('载荷是 5 键 × 3 作用域', !!sent && Object.keys(sent.body.limits).length === 5
    && Object.keys(sent.body.limits.reserve_credits).length === 3);
  check('空串=继承、0=关闭 原样传递（不被序列化成 0）',
    !!sent && sent.body.limits.reserve_credits.global === '500'
    && sent.body.limits.reserve_credits.intl === '' && sent.body.limits.reserve_credits.cn === '0');
  env = makeEnv();
  env.api.renderLimits(null);
  await env.api.saveLimits(env.st.btn);
  check('读不到现有配置时**不发请求**（防止一次盲存把已有阈值覆盖成 0）',
    env.st.posted.length === 0 && env.st.btn.disabled === true);

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
})().catch(e => { console.error('SUITE ERROR', e); process.exit(2); });
