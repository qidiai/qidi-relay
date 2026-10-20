/* dashboard.html 抽取工具（零依赖，node）—— 供 tests/_test_dashboard_*.js 复用。
 *
 * 解决两个「断言自身的薄弱处」：
 *   1. 锚点存在性：抽取用的是 indexOf(锚点)，锚点一旦被改动，indexOf 返回 -1，
 *      slice() 会**静默**给出空串 —— 断言照样「通过」（假绿）。requireAnchors() 会先失败。
 *   2. 纯函数区纯度：这批断言的前提是「抽出来的东西真的是纯的」（不碰 DOM/全局）。
 *      将来有人把渲染逻辑塞回纯函数区，桩一装就绿 —— assertPure() 用静态检查钉住。
 *
 * 用法：
 *   const { extract, requireAnchors, assertPure, region } = require('./_tools/dash_extract.js');
 *   const { code } = extract();
 *   const r = requireAnchors(code, ['const KEY_ID_LABELS = {', 'async function refreshInner(){'], 'Key 列');
 *   assertPure(r, 'Key 列');
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.dirname(path.dirname(__dirname));   // tests/_tools -> 仓根
const DASH = path.join(ROOT, 'dashboard.html');

function extract(htmlPath) {
  const html = fs.readFileSync(htmlPath || DASH, 'utf8');
  const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  return { html, code: blocks.join('\n'), blocks: blocks.length };
}

/* 按两个锚点切片；任一锚点缺失就抛（而不是返回空串）。
 * 同一个套件里多次调用时，失败信息会带上 label，便于定位是哪一组锚点被改动了。 */
function region(code, startAnchor, endAnchor, label) {
  const i = code.indexOf(startAnchor);
  const j = code.indexOf(endAnchor);
  if (i < 0) throw new Error('锚点缺失(起点): ' + JSON.stringify(startAnchor) + ' @ ' + label);
  if (j < 0) throw new Error('锚点缺失(终点): ' + JSON.stringify(endAnchor) + ' @ ' + label);
  if (j <= i) throw new Error('锚点顺序异常 @ ' + label + ' (start=' + i + ', end=' + j + ')');
  return code.slice(i, j);
}

function requireAnchors(code, anchors, label) {
  return region(code, anchors[0], anchors[1], label || 'region');
}

/* 纯函数区不得出现 DOM / 浏览器全局引用 —— 出现即说明「纯」的前提被破坏了。
 * 注意：允许出现字符串字面量里的同名文本（极少见），这里用「标识符位置」的粗判：
 * 排除注释行后再匹配。 */
const DOM_GLOBALS = ['document', 'window', 'localStorage', 'sessionStorage', 'fetch', 'navigator'];

function assertPure(source, label) {
  const lines = source.split('\n').filter(l => !/^\s*(\/\/|\*|\/\*)/.test(l));
  const hits = [];
  for (const g of DOM_GLOBALS) {
    const rx = new RegExp('(^|[^\\w.$])' + g + '\\s*[.\\[]');
    for (const l of lines) {
      if (rx.test(l)) { hits.push(g + '  @  ' + l.trim().slice(0, 70)); break; }
    }
  }
  return { ok: hits.length === 0, hits, label: label || 'region' };
}

/* 按**单个函数**判定纯度 —— 比整区判定准确得多。
 * 实测教训：棉花糖给的「Key 列 / by_key 档位」两个锚点区间里**本来就含 DOM 访问**
 *   （`document.getElementById('keyMatrix')` 之类），整区判定必然报 FAIL；
 *   而真正要钉的是「这几个**纯函数**不许碰 DOM」。所以按函数名逐个抽函数体再判。
 */
function fnBody(code, name) {
  const patterns = [
    new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\('),
    new RegExp('(?:const|let|var)\\s+' + name + '\\s*=\\s*(?:async\\s*)?(?:function|\\()'),
  ];
  for (const rx of patterns) {
    const m = rx.exec(code);
    if (!m) continue;
    let i = code.indexOf('{', m.index);
    if (i < 0) continue;
    let depth = 0, started = false;
    for (let k = i; k < code.length; k++) {
      const c = code[k];
      if (c === '{') { depth++; started = true; }
      else if (c === '}') { depth--; if (started && depth === 0) return code.slice(m.index, k + 1); }
    }
  }
  return null;
}

function assertPureFn(code, names, label) {
  const bad = [];
  for (const n of names) {
    const body = fnBody(code, n);
    if (body === null) { bad.push({ fn: n, why: '函数未找到' }); continue; }
    const r = assertPure(body, n);
    if (!r.ok) bad.push({ fn: n, why: r.hits });
  }
  return { ok: bad.length === 0, bad, label: label || 'fn-set' };
}

module.exports = { extract, region, requireAnchors, assertPure, assertPureFn, fnBody,
                   DOM_GLOBALS, ROOT, DASH };
