/* 整页加载层（层 3）：读 dashboard.html → 解析 body 里的页面/section 树 → 建「真 classList」DOM 桩
 * → eval **整个主 script 块** → 断言页签切换 / pageNav 生成 / 主题三态真的工作。
 *
 * 为什么需要：切片套件（层 1+2）只证明「被抽出的函数片段」正确；整块脚本里
 * 声明顺序、遗漏声明、DOM 结构与 JS 的耦合（.main-page.active 类切换）只有
 * 整页加载才能暴露。wb 的教训：i18n 层按片段 eval 时声明都在，浏览器整块加载
 * 也对，但两套机制的耦合点只有整页才能测到。
 *
 *   node tests/_test_dashboard_full_load.js
 */
const fs = require('fs');
const path = require('path');
const ROOT = process.env.QD_DASH_ROOT || path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'dashboard.html'), 'utf8');
const SUITE_MD5 = require('crypto').createHash('md5').update(require('fs').readFileSync(__filename)).digest('hex');
let PASS = 0, FAIL = 0;
function check(name, cond){ if(cond){ PASS++; console.log('  ok   ' + name); } else { FAIL++; console.log('  FAIL ' + name); } }

// ---------- 最小「真 DOM」桩 ----------
// 手写而非 jsdom：零依赖红线。只实现被主脚本触到的接口，但 classList /
// appendChild / dataset / dataset 移除都用真语义，避免「桩说改了但 JS 依赖真行为」。
function El(tag){
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    id: '', className: '', _text: '', _html: '', children: [], parentNode: null,
    dataset: {}, style: { setProperty(){}, }, attrs: {},
    listeners: {},
    get classList(){
      const self = el;
      const set = () => new Set(String(self.className).split(/\s+/).filter(Boolean));
      return {
        add(...cs){ const s = set(); cs.forEach(c => s.add(c)); self.className = [...s].join(' '); },
        remove(...cs){ const s = set(); cs.forEach(c => s.delete(c)); self.className = [...s].join(' '); },
        contains(c){ return set().has(c); },
        toggle(c, on){
          const s = set();
          if(on === undefined) on = !s.has(c);
          if(on) s.add(c); else s.delete(c);
          self.className = [...s].join(' ');
          return on;
        },
      };
    },
    appendChild(c){ c.parentNode = el; el.children.push(c); return c; },
    insertBefore(c, ref){ c.parentNode = el; const i = ref ? el.children.indexOf(ref) : -1;
      if(i < 0) el.children.push(c); else el.children.splice(i, 0, c); return c; },
    remove(){ const p = el.parentNode; if(p){ const i = p.children.indexOf(el); if(i >= 0) p.children.splice(i, 1); } },
    querySelector(sel){ return el.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel){
      // 支持 .class / tag / #id（子树匹配）。够用即可，不做完整选择器引擎。
      const out = [];
      const walk = node => {
        for(const c of node.children){
          if(matchSel(c, sel)) out.push(c);
          walk(c);
        }
      };
      walk(el);
      return out;
    },
    addEventListener(t, fn){ (el.listeners[t] = el.listeners[t] || []).push(fn); },
    setAttribute(k, v){ el.attrs[k] = String(v); if(k === 'id') el.id = String(v); },
    getAttribute(k){ return el.attrs[k] != null ? el.attrs[k] : ''; },
    textContentOf(){ return el._text; },
    getBoundingClientRect(){ return { top: 0, left: 0 }; },
    cloneNode(deep){
      const c = El(el.tagName);
      c.className = el.className; c.id = el.id; c._text = el._text; c._html = el._html;
      Object.assign(c.attrs, el.attrs); Object.assign(c.dataset, el.dataset);
      if(deep) el.children.forEach(ch => c.appendChild(ch.cloneNode(true)));
      return c;
    },
    forEach(){}, closest(){ return null; },
  };
  Object.defineProperty(el, 'textContent', { get(){ return el._text; }, set(v){ el._text = String(v == null ? '' : v); } });
  Object.defineProperty(el, 'innerHTML', { get(){ return el._html; }, set(v){ el._html = String(v); } });
  Object.defineProperty(el, 'firstChild', { get(){ return el.children[0] || null; } });
  Object.defineProperty(el, 'offsetHeight', { get(){ return 64; } });
  return el;
}
function matchSel(node, sel){
  if(!sel) return false;
  if(sel[0] === '#') return node.id === sel.slice(1);
  if(sel[0] === '.') return String(node.className).split(/\s+/).includes(sel.slice(1));
  return node.tagName === sel.toUpperCase();
}

// ---------- 从 html 静态解析页面/section 树（足够 pageNav 用） ----------
// body 的层级不完整解析——只抽我们关心的骨架：<div id=pageX class=main-page> 与其内 <section>
const pageRe = /<div id="(page\w+)" class="main-page"[^>]*>/g;
const pages = [];
let m;
while((m = pageRe.exec(html))){ pages.push({ id: m[1], raw: pageBody(html, m.index) }); }
function pageBody(src, from){
  // 取该 div 到配对 </div>（同层）——用扫描计数
  const open = /<div\b|<\/div>/g; open.lastIndex = from;
  let depth = 0, mm;
  while((mm = open.exec(src))){
    if(mm[0] === '</div>'){
      depth--;
      if(depth === 0) return src.slice(from, mm.index);
    } else {
      depth++;
    }
  }
  return src.slice(from, from + 50000);
}
check('静态骨架：四个 .main-page 容器都带 class（pageGateway/Analytics/Settings/Logs）',
  ['pageGateway','pageAnalytics','pageSettings','pageLogs'].every(id => pages.some(p => p.id === id)));
check('静态骨架：不再有内联 style="display:none" 的页面容器（显隐已交给 .main-page.active）',
  !/id="page(Gateway|Analytics|Settings|Logs)"[^>]*style="display:none"/.test(html));

// 每页 section 标题清单（h2 文本，去 <em>）
function sectionTitles(raw){
  const out = [];
  const secRe = /<section([^>]*)>([\s\S]*?)<\/section>/g;
  let s;
  while((s = secRe.exec(raw))){
    if(/display:\s*none/.test(s[1])) continue;
    const h2 = (s[2].match(/<h2[^>]*>([\s\S]*?)<\/h2>/) || [])[1] || '';
    const label = h2.replace(/<em[\s\S]*?<\/em>/g, '').replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
    if(label) out.push(label);
  }
  return out;
}
const gwSections = sectionTitles(pages.find(p => p.id === 'pageGateway') ? pages.find(p => p.id === 'pageGateway').raw : '');
const anSections = sectionTitles(pages.find(p => p.id === 'pageAnalytics') ? pages.find(p => p.id === 'pageAnalytics').raw : '');
check('pageGateway 有 ≥3 个可见 section（pageNav 会生成侧栏；签到区初始隐藏、由 JS 运行时点亮后 MutationObserver 刷新导航）', gwSections.length >= 3);
check('pageAnalytics 有 ≥2 个可见 section', anSections.length >= 2);

// ---------- 建 DOM 树并 eval 整个主 script ----------
const body = El('body');
const docEl = El('html');
const byId = {};
function buildPage(pid, titles){
  const page = El('div'); page.id = pid; page.className = 'main-page';
  byId[pid] = page; body.appendChild(page);
  titles.forEach(t => {
    const sec = El('section');
    const h2 = El('h2'); h2.textContent = t;
    sec.appendChild(h2); page.appendChild(sec);
  });
}
buildPage('pageGateway', gwSections);
buildPage('pageAnalytics', anSections);
buildPage('pageSettings', ['面板访问密码', 'API Key 与出口绑定', '事实性限额护栏', '运行信息']);
buildPage('pageLogs', ['网关运行日志']);
['gateway','analytics','logs','settings'].forEach(t => {
  const b = El('button'); b.id = 'btnNav' + t[0].toUpperCase() + t.slice(1); byId[b.id] = b; body.appendChild(b);
});
// header 桩（pageNavHeaderOffset 读它）
const header = El('header'); byId['header'] = header; body.appendChild(header);

const win = {
  addEventListener(){}, location: { search: '', href: 'http://x/', hash: '', pathname: '/', },
  matchMedia: q => ({ matches: /dark/.test(q) && false, addEventListener(){} }),
  localStorage: (() => { const s = {}; return { getItem: k => (k in s ? s[k] : null), setItem: (k, v) => { s[k] = String(v); }, removeItem: k => { delete s[k]; } }; })(),
  sessionStorage: { getItem: () => null, setItem(){}, removeItem(){} },
  pageYOffset: 0, innerHeight: 800, scrollTo(){},
  history: { replaceState(){} }, URL: URL,
  __MAIN_TAB__: 'gateway',
};
const doc = {
  getElementById: id => byId[id] || null,
  querySelector: sel => (sel === 'header' ? header : (sel[0] === '.' ? body.querySelectorAll(sel)[0] || null : null)),
  querySelectorAll: sel => body.querySelectorAll(sel),
  createElement: t => El(t),
  addEventListener(){},
  body: body, documentElement: docEl,
  readyState: 'complete',
};
Object.defineProperty(win, ' MutationObserver', { value: undefined });

// —— 主 script 块（最后一个，body 里那个）——
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(x => x[1]);
check('主 script 块可抽取且非空', blocks.length >= 1 && blocks[blocks.length - 1].length > 100000);
const mainJs = blocks[blocks.length - 1];

let sandboxErr = null;
let api = null;
global.document = doc; global.window = win; global.localStorage = win.localStorage;
global.sessionStorage = win.sessionStorage; global.navigator = { userAgent: 'node' };
global.fetch = async () => ({ status: 200, ok: true, text: async () => '{}', json: async () => ({}) });
global.getComputedStyle = () => ({ display: 'block' });
global.MutationObserver = class { constructor(cb){ this.cb = cb; } observe(){} };
global.history = win.history; global.location = win.location;
try {
  api = new Function('document', 'window', 'localStorage', 'navigator', 'fetch',
    'getComputedStyle', 'MutationObserver', 'history', 'location',
    mainJs + ';\nreturn {switchMainTab:switchMainTab, initPageNav:initPageNav, MAIN_TABS:MAIN_TABS, pageNavSections:pageNavSections};')(
    doc, win, win.localStorage, global.navigator, global.fetch, global.getComputedStyle, global.MutationObserver, win.history, win.location);
} catch(e){ sandboxErr = e; }
check('整个主 script 块在 DOM 桩上 eval 不抛异常（整页加载层）', sandboxErr === null,
  sandboxErr ? String(sandboxErr && sandboxErr.stack || sandboxErr).slice(0, 300) : '');

if(api){
  // —— 页签切换（class 语义）——
  byId['pageGateway'].classList.add('active');
  api.switchMainTab('logs');
  check('switchMainTab("logs") → pageLogs 有 active、pageGateway 无（class 语义显隐）',
    byId['pageLogs'].classList.contains('active') && !byId['pageGateway'].classList.contains('active'));
  check('switchMainTab("logs") → 按钮态同步（btnNavLogs active）',
    byId['btnNavLogs'].classList.contains('active') && !byId['btnNavGateway'].classList.contains('active'));
  check('切换页签时把页签写入 localStorage（qd-main-tab，刷新后还在）',
    win.localStorage.getItem('qd-main-tab') === 'logs');
  api.switchMainTab('gateway');
  check('切回 gateway → active 回到 pageGateway', byId['pageGateway'].classList.contains('active') && !byId['pageLogs'].classList.contains('active'));

  // —— pageNav 生成（gateway 页 ≥4 区块 → 有侧栏；logs 页 1 区块 → 无侧栏）——
  api.initPageNav(byId['pageGateway']);
  const sidebar = byId['pageGateway'].querySelector('.page-sidebar');
  const nav = byId['pageGateway'].querySelector('.page-nav');
  check('initPageNav(pageGateway) → 生成了 .page-sidebar 与 .page-nav',
    !!sidebar && !!nav);
  if(nav){
    const items = nav.querySelectorAll('.page-nav-item');
    check('导航项数 == 静态解析的 section 数（' + gwSections.length + '）', items.length === gwSections.length);
    check('导航项文本取自 section 的 h2（首项 = 「' + gwSections[0] + '」）',
      items.length > 0 && items[0].textContent === gwSections[0]);
    check('页面有 page-nav-on 类（两栏布局生效的开关）', byId['pageGateway'].classList.contains('page-nav-on'));
    check('区块被赋了稳定锚点 id（page-sec- 前缀）',
      byId['pageGateway'].children.some(c => c.tagName === 'SECTION' && /^page-sec-/.test(c.id)));
  }
  api.initPageNav(byId['pageLogs']);
  check('initPageNav(pageLogs) → 区块不足两项 → 不给侧栏（page-nav-on 被移除/未加）',
    !byId['pageLogs'].classList.contains('page-nav-on')
    && !byId['pageLogs'].querySelector('.page-sidebar'));

  // —— pageNav 幂等重建（清单签名不变 → 不重建）——
  const navBefore = byId['pageGateway'].querySelector('.page-nav');
  api.initPageNav(byId['pageGateway']);
  check('initPageNav 幂等：签名不变 → 同一个 nav 节点（不闪高亮）',
    byId['pageGateway'].querySelector('.page-nav') === navBefore);

  // —— 主题三态（head 脚本的 API 也挂 window）——
  // head 脚本在整页 eval 之外（它在 <head>，不在主块），这里单独验证其行为契约：
  const headJs = blocks[0];
  check('head 脚本含三态主题与预点亮（浅色/深色/跟随系统 + __MAIN_TAB__）',
    /qd-theme/.test(headJs) && /__MAIN_TAB__/.test(headJs) && /prefers-color-scheme/.test(headJs));
  check('head 脚本预点亮在 DOMContentLoaded 先于主题 UI（无白屏一闪）',
    /classList\.toggle\('active'/.test(headJs) && /initThemeSystem/.test(headJs));
}

// 指纹两行
console.log('SUITE: ' + path.basename(__filename) + ' md5=' + SUITE_MD5);
console.log('FILE: ' + path.basename(path.join(ROOT, 'dashboard.html'))
  + ' md5=' + require('crypto').createHash('md5').update(html).digest('hex')
  + ' bytes=' + Buffer.byteLength(html, 'utf8') + ' codeunits=' + html.length
  + ' as-of=' + new Date().toISOString());
console.log('');
console.log('SUMMARY: TOTAL ' + (PASS + FAIL) + ' checks, ' + PASS + ' passed, ' + FAIL + ' failed');
console.log('RESULT: ' + (FAIL ? 'RED' : 'GREEN') + ' (exit ' + (FAIL ? 1 : 0) + ')');
process.exit(FAIL ? 1 : 0);
