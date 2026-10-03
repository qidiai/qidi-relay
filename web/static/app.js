/* ═══════════════════════════════════════════════════════════════
   QiDi Relay v4 — 控制台前端逻辑
   模块：背景动画 / 图表 / 鉴权 / 状态 / 账号 / 统计 / 动态 / 日志 / 测试台
   ═══════════════════════════════════════════════════════════════ */

"use strict";

// ─── 工具 ──────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML.replace(/'/g, "&#39;").replace(/"/g, "&quot;");
}

function fmt(n) {
  n = Number(n || 0);
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(2) + "万";
  return n.toLocaleString();
}

function fmtFull(n) { return Number(n || 0).toLocaleString(); }

function toast(msg, isErr = false) {
  const zone = $("toast-zone");
  const t = document.createElement("div");
  t.className = "toast" + (isErr ? " err" : "");
  t.textContent = msg;
  zone.appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 350); }, 2800);
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    if (btn) { const old = btn.textContent; btn.textContent = "已复制 ✓"; setTimeout(() => btn.textContent = old, 1200); }
    toast("已复制到剪贴板");
  }).catch(() => toast("复制失败", true));
}

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (r.status === 401) { showGate(); throw new Error("未授权"); }
  return r;
}

// 数字滚动动画
const kpiPrev = {};
function animateNum(el, target, formatter = fmtFull) {
  const from = kpiPrev[el.id] || 0;
  kpiPrev[el.id] = target;
  if (from === target) { el.textContent = formatter(target); return; }
  const start = performance.now(), dur = 700;
  function step(now) {
    const p = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = formatter(Math.round(from + (target - from) * eased));
    if (p < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

// ─── 背景动画：透视网格 + 粒子流 ─────────────────────────────

(function bgAnimation() {
  const cv = $("bg-canvas");
  const ctx = cv.getContext("2d");
  let W, H, particles = [];

  function resize() {
    W = cv.width = innerWidth;
    H = cv.height = innerHeight;
  }
  resize();
  addEventListener("resize", resize);

  for (let i = 0; i < 70; i++) {
    particles.push({
      x: Math.random(), y: Math.random(),
      s: Math.random() * 1.6 + 0.4,
      v: Math.random() * 0.0006 + 0.0002,
      hue: Math.random() < 0.85 ? "0,229,255" : "236,111,217",
    });
  }

  let offset = 0;
  function frame() {
    ctx.clearRect(0, 0, W, H);

    // 深空渐变
    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, "#04070f");
    g.addColorStop(0.6, "#050a16");
    g.addColorStop(1, "#071022");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);

    // 透视网格（底部地平线）
    const horizon = H * 0.62;
    offset = (offset + 0.35) % 46;
    ctx.strokeStyle = "rgba(0,229,255,0.07)";
    ctx.lineWidth = 1;
    for (let i = -1; i < 16; i++) {
      const y = horizon + Math.pow(i + offset / 46, 2.1) * 34;
      if (y < horizon || y > H + 40) continue;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    const cx = W / 2;
    for (let i = -14; i <= 14; i++) {
      ctx.beginPath();
      ctx.moveTo(cx + i * 60, horizon);
      ctx.lineTo(cx + i * W * 0.14, H);
      ctx.stroke();
    }
    // 地平线辉光
    const hg = ctx.createLinearGradient(0, horizon - 30, 0, horizon + 30);
    hg.addColorStop(0, "rgba(0,229,255,0)");
    hg.addColorStop(0.5, "rgba(0,229,255,0.12)");
    hg.addColorStop(1, "rgba(0,229,255,0)");
    ctx.fillStyle = hg;
    ctx.fillRect(0, horizon - 30, W, 60);

    // 上升粒子
    for (const p of particles) {
      p.y -= p.v;
      if (p.y < -0.02) { p.y = 1.02; p.x = Math.random(); }
      const px = p.x * W, py = p.y * H;
      ctx.fillStyle = `rgba(${p.hue},${0.25 + p.s * 0.2})`;
      ctx.fillRect(px, py, p.s, p.s * 3.5);
    }

    requestAnimationFrame(frame);
  }
  frame();
})();

// ─── 时钟 ────────────────────────────────────────────────────

setInterval(() => {
  $("clock").textContent = new Date().toTimeString().slice(0, 8);
}, 1000);
$("clock").textContent = new Date().toTimeString().slice(0, 8);

function fmtUptime(sec) {
  sec = Number(sec || 0);
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600),
        m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// ─── 服务状态 ────────────────────────────────────────────────

let healthOk = false;

async function loadHealth() {
  try {
    const r = await fetch("/health");
    const d = await r.json();
    healthOk = true;
    $("status-led").className = "led on";
    $("status-text").textContent = "ONLINE";
    $("endpoint-url").textContent = d.endpoint;
    $("ver").textContent = (d.version || "4").split(".")[0];
    $("bver").textContent = d.version || "?";
    animateNum($("kpi-uptime"), d.uptime_seconds, fmtUptime);
    kpiPrev["kpi-uptime"] = d.uptime_seconds; // 不滚动的特殊处理
    $("kpi-accounts").textContent = `${d.accounts_enabled}/${d.accounts}`;
    $("kpi-acc-sub").textContent = `${d.accounts} ACCOUNTS LOADED`;
    renderSnippet();
  } catch (e) {
    healthOk = false;
    $("status-led").className = "led off";
    $("status-text").textContent = "OFFLINE";
  }
}

// ─── 鉴权 ────────────────────────────────────────────────────

let isAdmin = false;

function showGate() {
  if (!isAdmin) $("login-gate").style.display = "flex";
}
function hideGate() { $("login-gate").style.display = "none"; }

async function checkAuth() {
  try {
    const r = await fetch("/api/auth-check");
    const d = await r.json();
    isAdmin = !!d.authorized;
  } catch (e) { isAdmin = false; }
  if (isAdmin) {
    hideGate();
    $("logoutBtn").style.display = "inline-block";
    loadAll();
  } else {
    showGate();
  }
}

async function doLogin() {
  const pwd = $("login-pwd").value;
  const r = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pwd }),
  });
  const d = await r.json();
  if (d.ok) {
    isAdmin = true;
    hideGate();
    $("logoutBtn").style.display = "inline-block";
    $("login-pwd").value = "";
    $("login-err").textContent = "";
    toast("验证通过，控制台已解锁");
    loadAll();
  } else {
    $("login-err").textContent = d.error || "密码错误";
  }
}
$("login-pwd")?.addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });

async function logout() {
  await fetch("/api/logout", { method: "POST" });
  isAdmin = false;
  $("logoutBtn").style.display = "none";
  toast("已退出登录");
  showGate();
}

// ─── 账号矩阵 ────────────────────────────────────────────────

// 上游账号信息（/api/accounts/upstream，服务端 120s 缓存；旧后端未重启时静默跳过）
let upstreamMap = {};      // user_id -> 上游信息
let providersSupported = true; // /api/providers/info 不存在（404）时置 false 并隐藏面板

function fmtSpend(n) {
  return Number(n || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// 从 LiteLLM key_alias（Codely-{email}-{uid}-{org}-prod-cli）提取账号邮箱，便于识别"这是哪个号"
function aliasEmail(alias) {
  const m = /^(?:Codely-)?(.+?)-(\d+)-(?:\d+-)?prod/.exec(alias || "");
  return m ? m[1] : (alias || "");
}

async function loadAccounts() {
  if (!isAdmin) return;
  try {
    const r = await api("/api/accounts");
    const accounts = await r.json();
    const grid = $("account-grid");
    const online = accounts.filter(a => a.enabled && !a.budget_exceeded).length;
    $("account-count-tag").textContent = `${online}/${accounts.length} ONLINE`;

    if (!accounts.length) {
      grid.innerHTML = '<div class="acc-empty">— 暂无账号，点击右上角「添加账号」—</div>';
      return;
    }
    grid.innerHTML = accounts.map(a => {
      const hrs = Number(a.token_remaining_hours || 0);
      const expCls = hrs > 72 ? "ok" : hrs > 24 ? "warn" : "crit";
      const expPct = Math.max(2, Math.min(100, hrs / 720 * 100)); // 按 30 天满格
      const cdMin = Math.ceil((a.quota_cooldown_left || 0) / 60);
      const stateBadge = a.budget_exceeded
        ? `<span class="badge quota" title="冷却到期自动恢复，无需手动启用">${esc(a.cooldown_reason || "配额用尽")} · ${cdMin}分</span>`
        : a.enabled ? '<span class="badge on">运行中</span>' : '<span class="badge off">已禁用</span>';
      const glmBadge = a.has_glm53 ? '<span class="badge glm">GLM-5.3</span>' : "";
      // 0831：订阅/免费组徽章 + 邮箱（来自 JWT，本地解析），一眼区分哪个号是订阅
      // 0901 三档："" = Lite 订阅 / free = 免费号 / pro = Pro 订阅
      const groupBadge = a.group === "free"
        ? '<span class="badge ext" title="免费额度池">免费组</span>'
        : a.group === "pro"
          ? '<span class="badge glm" title="Pro 订阅额度池">PRO订阅</span>'
          : '<span class="badge glm" title="Lite 订阅额度池">Lite订阅</span>';
      const emailLine = a.email ? `<div class="acc-meta"><span>📧 ${esc(a.email)}</span></div>` : "";
      const load = (inflightData.loads || {})[a.user_id] || 0;
      const loadBadge = load > 0 ? `<span class="badge ext">进行中 ${load}</span>` : "";
      const up = upstreamMap[a.user_id];
      const upLine = up && up.ok
        ? `<div class="acc-meta"><span title="LiteLLM key 级消费">KEY消费 <b>${fmtSpend(up.spend)}</b></span>${up.team_spend != null ? `<span title="团队级消费（30天周期）">团队消费 <b>${fmtSpend(up.team_spend)}</b></span>` : ''}<span>${esc(aliasEmail(up.key_alias))}</span></div>${up.plan_product ? `<div class="acc-meta"><span>套餐 <b>${esc(up.plan_product)}</b></span>${up.team_budget_reset_at ? `<span>重置 ${esc(up.team_budget_reset_at.slice(5,16))}</span>` : ''}${up.plan_valid_to ? `<span>到期 ${esc(up.plan_valid_to.slice(0,10))}</span>` : ''}</div>` : ''}`
        : (up ? `<div class="acc-meta"><span class="dim">${esc(up.error || '上游信息查询失败')}</span></div>` : '');
      const loginBadge = up && up.blocked
        ? '<span class="badge quota">已封禁</span>'
        : (a.login_status === "已过期" || a.token_remaining_hours <= 0)
          ? '<span class="badge quota">已过期</span>'
          : '<span class="badge on">已登录</span>';
      return `
      <div class="acc-card ${a.enabled ? "" : "disabled"}">
        <div class="acc-top">
          <span class="acc-led ${a.enabled && !a.budget_exceeded ? "on" : "off"}"></span>
          <span class="acc-name">${esc(a.username || "未命名")}</span>
          <span class="acc-id">#${esc(a.user_id)}</span>
        </div>
        <div class="acc-badges">${loginBadge}${stateBadge}${groupBadge}${glmBadge}${loadBadge}</div>
        ${emailLine}
        <div class="acc-meta"><span>调用 <b>${fmtFull(a.use_count)}</b> 次</span><span>KEY 剩余 <b>${esc(a.token_remaining_hours)}h</b></span></div>
        ${upLine}
        <div class="exp-bar"><i class="${expCls}" style="width:${expPct}%"></i></div>
        <div class="acc-meta"><span>到期 ${esc(a.token_expires)}</span><span>${a.last_used ? "最近调用 " + esc(String(a.last_used).slice(5, 16).replace("T", " ")) : "未使用"}</span></div>
        <div class="acc-actions">
          <button class="btn tiny ${a.enabled ? "danger" : "primary"}" onclick="toggleAccount('${esc(a.user_id)}', ${!a.enabled})">${a.enabled ? "禁用" : "启用"}</button>
          <button class="btn tiny ghost" onclick="removeAccount('${esc(a.user_id)}')">删除</button>
        </div>
      </div>`;
    }).join("");
  } catch (e) { /* 忽略瞬时错误 */ }
}

async function toggleAccount(uid, enabled) {
  await api("/api/account/toggle", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: uid, enabled }),
  });
  toast(enabled ? `账号 ${uid} 已启用` : `账号 ${uid} 已禁用`);
  loadAccounts(); loadHealth();
}

async function removeAccount(uid) {
  if (!confirm(`确认删除账号 ${uid}？此操作立即生效。`)) return;
  await api("/api/account/remove", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: uid }),
  });
  toast(`账号 ${uid} 已删除`);
  loadAccounts(); loadHealth();
}

async function addAccount() {
  const userId = $("addUserId").value.trim(), token = $("addToken").value.trim();
  if (!userId || !token) { toast("账号ID 和 Token 不能为空", true); return; }
  const r = await api("/api/account/add", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId, token,
      username: $("addUsername").value.trim(),
      org_id: $("addOrgId").value.trim(),
    }),
  });
  const d = await r.json();
  if (d.ok) {
    toast("账号已保存");
    closeModal("addModal");
    ["addUserId", "addToken", "addUsername", "addOrgId"].forEach(id => $(id).value = "");
    loadAccounts(); loadHealth();
  } else toast(d.error || "保存失败", true);
}

async function refreshAccounts() {
  await api("/api/account/refresh", { method: "POST" });
  toast("已重载账号并清空 Key 缓存");
  loadAccounts();
}

async function checkModels() {
  toast("正在检测各账号模型…");
  await api("/api/models/check", { method: "POST" });
  toast("模型检测完成");
  loadAccounts();
}

// ─── 注水检测（/api/watercheck；旧后端未重启时静默跳过） ──────

let waterSupported = true;
let waterData = { probes: [], passive: [], perf: [], canary_count: 0 };
let waterBusy = false;

function ensureWaterPanel() {
  let sec = $("panel-water");
  if (sec) return sec;
  // 插到「进行中请求」面板前面
  const anchor = document.getElementById("panel-inflight") ||
    Array.from(document.querySelectorAll(".panel"))
      .find(p => (p.querySelector("h2") || {}).textContent && p.querySelector("h2").textContent.includes("实时动态"));
  if (!anchor || !anchor.parentNode) return null;
  sec = document.createElement("section");
  sec.className = "panel wide";
  sec.id = "panel-water";
  sec.innerHTML = `
    <div class="panel-head">
      <h2>注水检测 <span class="tag" id="water-tag">WATER CHECK</span></h2>
      <div class="head-actions">
        <select id="water-model" class="btn tiny" style="background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);color:inherit">
          <option value="GLM-5.3">GLM-5.3</option>
        </select>
        <button class="btn tiny primary" id="water-run-btn" onclick="runWaterCheck()" title="对选定模型的每个账号发 3 道金丝雀题，核对模型名与分词器指纹">立即检测</button>
      </div>
    </div>
    <div class="panel-body">
      <div id="water-summary" class="acc-empty">尚未检测——被动观测零成本运行中，点「立即检测」主动探一次</div>
      <div id="water-probe-list" class="inflight-list"></div>
      ${"" /* passive events rendered below */}
      <div id="water-passive" style="margin-top:10px"></div>
      <p class="hint">判定规则：<b>模型名不符</b> = 上游返回的 model 与请求不一致（替换实锤）；<b>指纹漂移&gt;8%</b> = 同一题面分词器计数漂移，疑似换模型；<b>金丝雀失败</b> = 固定三题（复读/算术/常识）答错。被动观测每次转发自动进行，探针间隔 ≥30s。</p>
    </div>`;
  anchor.parentNode.insertBefore(sec, anchor);
  return sec;
}

function waterSeverityBadge(sev) {
  const map = {
    watered: ['<span class="badge quota">⚠ 注水嫌疑</span>', "crit"],
    suspect: ['<span class="badge off">存疑</span>', "warn"],
    ok: ['<span class="badge on">正常</span>', "ok"],
    unknown: ['<span class="badge ext">无数据</span>', ""],
  };
  return map[sev] || map.unknown;
}

function renderWater() {
  const sec = ensureWaterPanel();
  if (!sec) return;
  const probes = waterData.probes || [];
  const passive = waterData.passive || [];
  const perf = waterData.perf || [];
  // 汇总条
  const nWatered = probes.filter(p => p.severity === "watered").length;
  const nSuspect = probes.filter(p => p.severity === "suspect").length;
  const nOk = probes.filter(p => p.severity === "ok").length;
  $("water-tag").textContent = nWatered ? `${nWatered} ALERT` : (probes.length ? `${nOk}/${probes.length} PASS` : "WATER CHECK");
  const sumEl = $("water-summary");
  if (probes.length) {
    sumEl.className = "";
    sumEl.innerHTML = `最近探测：<b>${nOk}</b> 正常 · <b>${nSuspect}</b> 存疑 · <b style="color:var(--crit,#f66)">${nWatered}</b> 注水嫌疑` +
      (passive.length ? ` · 被动捕获 <b>${passive.length}</b> 条可疑事件` : "") +
      (perf.length ? ` · 性能画像 ${perf.length} 组` : "");
    sumEl.style.cssText = "font-size:12px;padding:6px 10px;color:var(--muted,#9ab)";
  } else if (passive.length) {
    sumEl.className = "";
    sumEl.innerHTML = `尚未主动探针，但被动观测已捕获 <b style="color:var(--crit,#f66)">${passive.length}</b> 条模型名不符事件`;
    sumEl.style.cssText = "font-size:12px;padding:6px 10px;color:var(--muted,#9ab)";
  } else {
    sumEl.className = "acc-empty";
    sumEl.style.cssText = "";
  }
  // 探针结果行
  const box = $("water-probe-list");
  if (probes.length) {
    box.innerHTML = probes.map(p => {
      const [badge, led] = waterSeverityBadge(p.severity);
      const canary = (p.canaries || []).map(c =>
        `<span class="if-idle ${c.passed ? "ok" : "crit"}">${esc(c.title)}${c.passed ? "✓" : "✗"}</span>`).join(" ");
      const fp = p.fingerprint != null
        ? `<span class="if-bytes">指纹 ${p.fingerprint}${p.drift_pct != null ? `（漂移 ${p.drift_pct}%）` : ""}</span>`
        : "";
      const notes = (p.notes || []).map(n => esc(n)).join("；");
      return `
      <div class="inflight-row" title="${esc(notes)}">
        <span class="led ${led}" style="width:8px;height:8px;border-radius:50%;flex-shrink:0"></span>
        <span class="if-model">${esc(p.model)}</span>
        <span class="if-uid">${p.kind === "provider" ? "外部源 " : "账号 "}${esc(p.user_id)}</span>
        ${badge}
        ${canary}
        ${fp}
        <span class="if-time">${esc(p.time || "")}</span>
      </div>`;
    }).join("");
  } else {
    box.innerHTML = "";
  }
  // 被动事件
  const pEl = $("water-passive");
  if (passive.length) {
    pEl.innerHTML = `<div class="acc-meta" style="margin-bottom:4px"><span class="dim">被动捕获（模型名不符，最近 20 条）</span></div>` +
      passive.map(e => `
      <div class="inflight-row" title="${esc(e.note || "")}">
        <span class="led crit" style="width:8px;height:8px;border-radius:50%;flex-shrink:0"></span>
        <span class="if-model">${esc(e.model)}</span>
        <span class="if-uid">账号 ${esc(e.user_id)}</span>
        <span class="if-idle crit">返回 ${esc(e.returned)}</span>
        <span class="if-time">${esc(e.time)}</span>
      </div>`).join("");
  } else {
    pEl.innerHTML = "";
  }
}

async function loadWater() {
  if (!isAdmin || !waterSupported) return;
  try {
    const r = await api("/api/watercheck");
    if (r.status === 404) { waterSupported = false; return; }
    waterData = await r.json();
    renderWater();
    loadWaterModels();
  } catch (e) { /* 忽略瞬时错误 */ }
}

// 可探测模型清单：主账号池 + 外部 Provider（agnes / atomcode 等），分组下拉。
// 旧后端无 /api/watercheck/models 时保留静态回退（GLM-5.3）。
async function loadWaterModels() {
  const sel = $("water-model");
  if (!sel) return;
  try {
    const r = await api("/api/watercheck/models");
    if (r.status === 404 || !r.ok) return;
    const d = await r.json();
    const pool = d.pool || [];
    const provs = d.providers || [];
    const flat = [...pool, ...provs.flatMap(p => p.models || [])];
    if (!flat.length) return;
    // 选项无变化时不动 DOM（避免打断用户正在展开的下拉框）
    const cur = Array.from(sel.options).map(o => o.value).join("\u0001");
    if (cur === flat.join("\u0001")) return;
    const keep = sel.value;
    const groups = [];
    if (pool.length) {
      groups.push(`<optgroup label="主账号池">` +
        pool.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("") + `</optgroup>`);
    }
    for (const p of provs) {
      if (!(p.models || []).length) continue;
      groups.push(`<optgroup label="${esc(p.name)} · 外部源">` +
        p.models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("") + `</optgroup>`);
    }
    sel.innerHTML = groups.join("");
    if (Array.from(sel.options).some(o => o.value === keep)) sel.value = keep;
    else sel.value = flat.includes("GLM-5.3") ? "GLM-5.3" : flat[0];
  } catch (e) { /* 忽略瞬时错误 */ }
}

async function runWaterCheck() {
  if (!isAdmin || waterBusy) return;
  const model = ($("water-model") || {}).value || "GLM-5.3";
  const btn = $("water-run-btn");
  waterBusy = true;
  if (btn) { btn.disabled = true; btn.textContent = "探测中…"; }
  toast(`正在对 ${model} 发金丝雀探针（每账号 3 题）…`);
  try {
    const r = await api("/api/watercheck/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    });
    if (r.status === 429) { toast("探针间隔需 ≥30s，请稍候", true); return; }
    if (!r.ok) { toast(`检测失败（HTTP ${r.status}）`, true); return; }
    waterData = await r.json();
    waterData.probes = waterData.results || [];
    delete waterData.results;
    renderWater();
    const nWatered = waterData.probes.filter(p => p.severity === "watered").length;
    if (nWatered) toast(`⚠ ${model} 有 ${nWatered} 个目标存在注水嫌疑！`, true);
    else toast(`${model} 全部通过（${waterData.probes.length} 个目标）`);
  } catch (e) {
    toast("注水检测请求异常: " + e, true);
  } finally {
    waterBusy = false;
    if (btn) { btn.disabled = false; btn.textContent = "立即检测"; }
  }
}

// ─── 上游账号信息 & 外部账号面板 ─────────────────────────────

// 进行中请求（/api/inflight；旧后端未重启时静默跳过）
let inflightSupported = true;
let inflightData = { requests: [], total: 0, loads: {} };

function ensureInflightPanel() {
  let sec = $("panel-inflight");
  if (sec) return sec;
  // 插到「实时动态」面板前面
  const anchor = Array.from(document.querySelectorAll(".panel"))
    .find(p => (p.querySelector("h2") || {}).textContent && p.querySelector("h2").textContent.includes("实时动态"));
  if (!anchor || !anchor.parentNode) return null;
  sec = document.createElement("section");
  sec.className = "panel wide";
  sec.id = "panel-inflight";
  sec.innerHTML = `
    <div class="panel-head">
      <h2>进行中请求 <span class="tag" id="inflight-count-tag">0 ACTIVE</span></h2>
      <div class="head-actions">
        <span class="hint" id="inflight-loads" style="font-size:11px"></span>
      </div>
    </div>
    <div class="panel-body">
      <div id="inflight-list" class="inflight-list">
        <div class="acc-empty">当前没有进行中的请求</div>
      </div>
      <p class="hint">流式请求会实时显示已耗时与最近收字时间；「静默」列持续增长说明上游卡住（120 秒无数据会自动熔断重试由客户端决定）。</p>
    </div>`;
  anchor.parentNode.insertBefore(sec, anchor);
  return sec;
}

function fmtDur(sec) {
  sec = Number(sec || 0);
  if (sec < 60) return sec.toFixed(0) + "s";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function renderInflight() {
  const sec = ensureInflightPanel();
  if (!sec) return;
  const reqs = inflightData.requests || [];
  $("inflight-count-tag").textContent = `${reqs.length} ACTIVE`;
  const loads = inflightData.loads || {};
  $("inflight-loads").textContent = "账号负载 " +
    Object.entries(loads).map(([uid, n]) => `${uid}:${n}`).join("  ");
  const box = $("inflight-list");
  if (!reqs.length) {
    box.innerHTML = '<div class="acc-empty">当前没有进行中的请求</div>';
    return;
  }
  box.innerHTML = reqs.map(r => {
    const idleCls = r.idle > 60 ? "crit" : (r.idle > 30 ? "warn" : "ok");
    return `
    <div class="inflight-row">
      <span class="led ${idleCls}" style="width:8px;height:8px;border-radius:50%;flex-shrink:0"></span>
      <span class="if-model">${esc(r.model)}</span>
      <span class="if-uid">账号 ${esc(r.user_id)}${r.stream ? " · 流式" : ""}</span>
      <span class="if-time">${fmtDur(r.elapsed)}</span>
      <span class="if-idle ${idleCls}">静默 ${fmtDur(r.idle)}</span>
      <span class="if-bytes">${fmtFull(r.bytes)}B</span>
    </div>`;
  }).join("");
}

async function loadInflight() {
  if (!isAdmin || !inflightSupported) return;
  try {
    const r = await api("/api/inflight");
    if (r.status === 404) { inflightSupported = false; return; }
    inflightData = await r.json();
    renderInflight();
    loadAccounts(); // 账号卡片负载徽章同步刷新
  } catch (e) { /* 忽略瞬时错误 */ }
}

async function loadUpstream() {
  if (!isAdmin) return;
  try {
    const r = await api("/api/accounts/upstream");
    if (r.status === 404) return; // 旧后端（尚未重启）没有此接口，静默跳过
    const d = await r.json();
    upstreamMap = {};
    for (const a of d.accounts || []) upstreamMap[a.user_id] = a;
    loadAccounts(); // 用上游数据重渲染卡片
  } catch (e) { /* 忽略瞬时错误 */ }
}

// 面板 DOM 由 JS 动态创建（静态文件免重启生效；老进程缓存的 index.html 不含此节点）
function ensureProvidersPanel() {
  let sec = $("panel-providers");
  if (sec) return sec;
  const anchor = $("panel-accounts");
  if (!anchor || !anchor.parentNode) return null;
  sec = document.createElement("section");
  sec.className = "panel wide";
  sec.id = "panel-providers";
  sec.innerHTML = `
    <div class="panel-head">
      <h2>外部账号 <span class="tag" id="provider-count-tag">EXTERNAL</span></h2>
      <div class="head-actions">
        <button class="btn tiny" onclick="loadProvidersInfo()" title="重新查询外部账号信息（服务端缓存 120s）">刷新</button>
      </div>
    </div>
    <div class="panel-body">
      <div class="account-grid" id="provider-grid"></div>
      <p class="hint">外部 Provider 在 config.json 的 providers 节配置（agnes 等支持 api_keys 多 key 轮换）；订阅/用量取自其 OpenAI 计费接口，不支持的端点会如实标注。静态 Key 也可在下方卡片里直接增删（自动备份 config.json，热生效，响应只回脱敏预览）。</p>
    </div>`;
  anchor.parentNode.insertBefore(sec, anchor.nextSibling);
  return sec;
}

function renderProviderCard(p) {
  const host = (p.base_url || "").replace(/^https?:\/\//, "");
  const stateBadge = p.ok
    ? '<span class="badge on">已连接</span>'
    : '<span class="badge quota">不可达</span>';
  const keyBadge = p.key_count > 1 ? `<span class="badge glm">${p.key_count} key 轮换</span>` : "";
  const sub = p.subscription || null;
  const payBadge = sub && sub.has_payment_method ? '<span class="badge glm">已绑支付方式</span>' : "";
  const modelBadges = (p.models || []).map(m => `<span class="badge ext">${esc(m)}</span>`).join("");
  // AtomCode 多账号列表 + 凭证上传表单
  let acSection = "";
  if (p.atomcode && p.accounts) {
    const rows = p.accounts.map(a => {
      const st = a.ok && a.logged_in
        ? `<span class="badge on">正常 ${a.token_valid_days}天</span>`
        : `<span class="badge quota">${esc(a.error || "未登录")}</span>`;
      const del = a.label && p.account_count > 1
        ? `<button class="btn tiny danger" onclick="removeAtomcodeAccount('${esc(a.label)}')">移除</button>` : "";
      return `<div class="ac-acct"><span>${esc(a.label || a.user_id || "?")}</span>${st}${del}</div>`;
    }).join("");
    acSection = `<div class="ac-accts">${rows}</div>
      <div class="ac-upload">
        <input type="text" id="ac-label-${esc(p.name)}" placeholder="账号备注（如：小号）">
        <textarea id="ac-content-${esc(p.name)}" rows="3" placeholder="粘贴 auth.toml 全文（另一台电脑的 C:\\Users\\用户\\.atomcode\\auth.toml）"></textarea>
        <button class="btn tiny primary" onclick="uploadAtomcode('${esc(p.name)}')">上传凭证</button>
      </div>`;
  }
  // Cline 多账号列表（凭证跟随官方客户端登录态，无上传/移除操作）
  let clSection = "";
  if (p.cline && p.accounts) {
    const rows = p.accounts.map(a => {
      const st = a.ok
        ? (a.expires_at_min > 0
            ? `<span class="badge on">在线 · token ${a.expires_at_min}min 后过期</span>`
            : `<span class="badge on">在线 · token 已过期（待客户端/代理刷新）</span>`)
        : `<span class="badge quota">${esc(a.error || "未登录")}</span>`;
      const cd = a.cooldown_min_left > 0
        ? `<span class="badge quota">冷却 ${a.cooldown_min_left}min（${esc(a.cooldown_note || "限流/空响应")}）</span>` : "";
      return `<div class="ac-acct"><span>${esc(a.label || a.user_id || "?")}</span>${st}${cd}</div>`;
    }).join("");
    clSection = `<div class="ac-accts">${rows}</div>`;
  }
  // 静态 Provider 的 API Key 自助管理（卡片内增/删；AtomCode/Cline 通道不适用）
  let keySection = "";
  if (!p.atomcode && !p.cline) {
    const krows = (p.key_entries || []).map(k =>
      `<div class="ac-acct"><span title="脱敏预览 ${esc(k.preview)}">${esc(k.label)} · ${esc(k.preview)}</span>` +
      `<button class="btn tiny danger" onclick="removeProviderKey('${esc(p.name)}','${esc(k.label)}')">移除</button></div>`).join("");
    keySection = `<div class="ac-accts">${krows || '<div class="ac-acct"><span class="dim">暂无 Key</span></div>'}</div>
      <div class="ac-upload">
        <input type="text" id="pk-label-${esc(p.name)}" placeholder="备注（可选，如：会员2）">
        <input type="text" id="pk-key-${esc(p.name)}" placeholder="粘贴 API Key（sk-...）">
        <button class="btn tiny primary" onclick="addProviderKey('${esc(p.name)}')">添加 Key</button>
      </div>`;
  }
  // OpenAI 计费口径：total_usage 单位为美分
  const usageTxt = p.usage && typeof p.usage.total_usage === "number"
    ? "$" + fmtSpend(p.usage.total_usage / 100)
    : `不可用（HTTP ${p.usage_status || "—"}）`;
  const limitTxt = sub
    ? (sub.hard_limit_usd != null && sub.hard_limit_usd < 1e8 ? `硬限额 <b>$${fmtFull(sub.hard_limit_usd)}</b>` : "限额 <b>不限</b>")
    : `订阅信息不可用（HTTP ${p.subscription_status || "—"}）`;
  return `
  <div class="acc-card ${p.ok ? "" : "disabled"}">
    <div class="acc-top">
      <span class="acc-led ${p.ok ? "on" : "off"}"></span>
      <span class="acc-name">${esc(p.name)}</span>
      <span class="acc-id">${esc(host)}</span>
    </div>
    <div class="acc-badges">${stateBadge}${keyBadge}${payBadge}${modelBadges}</div>
    ${(p.atomcode || p.cline) ? "" : `<div class="acc-meta"><span>累计用量 <b>${usageTxt}</b></span><span>${limitTxt}</span></div>`}
    ${acSection}
    ${clSection}
    ${keySection}
    ${p.ok ? "" : `<div class="acc-meta"><span class="dim">${esc(p.error || "连接失败")}</span></div>`}
  </div>`;
}

async function uploadAtomcode(pname) {
  const label = ($(`ac-label-${pname}`)?.value || "").trim();
  const content = ($(`ac-content-${pname}`)?.value || "").trim();
  if (!content) { toast("请粘贴 auth.toml 全文"); return; }
  try {
    const r = await api("/api/atomcode/upload", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, content }),
    });
    const d = await r.json();
    if (r.ok && !d.error) {
      toast(`已${d.updated ? "更新" : "添加"}账号「${d.label}」（共 ${d.account_count} 个）`);
      $(`ac-content-${pname}`).value = "";
      $(`ac-label-${pname}`).value = "";
      loadProvidersInfo();
    } else {
      toast(d.error || "上传失败");
    }
  } catch (e) { toast("上传失败: " + e.message); }
}

async function removeAtomcodeAccount(label) {
  if (!confirm(`移除 AtomCode 账号「${label}」？`)) return;
  try {
    const r = await api("/api/atomcode/remove", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    });
    const d = await r.json();
    if (r.ok && !d.error) {
      toast(`已移除「${d.removed}」（剩 ${d.account_count} 个）`);
      loadProvidersInfo();
    } else {
      toast(d.error || "移除失败");
    }
  } catch (e) { toast("移除失败: " + e.message); }
}

async function addProviderKey(pname) {
  const label = ($(`pk-label-${pname}`)?.value || "").trim();
  const key = ($(`pk-key-${pname}`)?.value || "").trim();
  if (!key) { toast("请先粘贴 API Key"); return; }
  try {
    const r = await api("/api/provider/key/add", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: pname, key, label }),
    });
    const d = await r.json();
    if (r.ok && !d.error) {
      toast(`已添加 Key「${d.label}」（共 ${d.key_count} 个）`);
      $(`pk-key-${pname}`).value = ""; $(`pk-label-${pname}`).value = "";
      loadProvidersInfo();
    } else toast(d.error || "添加失败", true);
  } catch (e) { toast("添加失败: " + e.message, true); }
}

async function removeProviderKey(pname, label) {
  if (!confirm(`移除 Provider「${pname}」的 Key「${label}」？`)) return;
  try {
    const r = await api("/api/provider/key/remove", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: pname, label }),
    });
    const d = await r.json();
    if (r.ok && !d.error) {
      toast(d.warning || `已移除「${d.removed}」（剩 ${d.key_count} 个）`);
      loadProvidersInfo();
    } else toast(d.error || "移除失败", true);
  } catch (e) { toast("移除失败: " + e.message, true); }
}

async function loadProvidersInfo() {
  if (!isAdmin || !providersSupported) return;
  try {
    const r = await api("/api/providers/info");
    if (r.status === 404) { providersSupported = false; return; } // 旧后端无此接口
    const d = await r.json();
    const provs = d.providers || [];
    if (!provs.length) return; // 未配置外部 provider，不显示面板
    const sec = ensureProvidersPanel();
    if (!sec) return;
    $("provider-count-tag").textContent = `${provs.length} LINKED`;
    $("provider-grid").innerHTML = provs.map(renderProviderCard).join("");
  } catch (e) { /* 忽略瞬时错误 */ }
}

// ─── API Key 用量（朋友共享）────────────────────────────────

let keysSupported = true;

function ensureKeysPanel() {
  let sec = $("panel-keys");
  if (sec) return sec;
  const anchor = $("panel-providers") || $("panel-accounts");
  if (!anchor || !anchor.parentNode) return null;
  sec = document.createElement("section");
  sec.className = "panel wide";
  sec.id = "panel-keys";
  sec.innerHTML = `
    <div class="panel-head">
      <h2>API Key 用量 <span class="tag" id="keys-tag">SHARED KEYS</span></h2>
      <div class="head-actions">
        <button class="btn tiny" onclick="loadKeysInfo()" title="刷新各 key 今日用量">刷新</button>
      </div>
    </div>
    <div class="panel-body">
      <div id="keys-grid" class="account-grid"></div>
      <p class="hint">在 config.json 的 "api_keys" 配置共享 key（支持 {"key","label","daily_token_limit"} 对象格式），每日零点自动清零；超限的 key 请求返回 429。列表按今日用量降序。</p>
    </div>`;
  anchor.parentNode.insertBefore(sec, anchor.nextSibling);
  return sec;
}

function renderKeyCard(k) {
  const limit = k.daily_token_limit;
  const used = k.today_tokens || 0;
  let pct = 0, quotaBadge = '<span class="badge off">无限额</span>';
  if (limit) {
    pct = Math.min(100, Math.round(used / limit * 100));
    quotaBadge = pct >= 100
      ? '<span class="badge quota">已达限额</span>'
      : `<span class="badge glm">${pct}%</span>`;
  }
  const models = Object.entries(k.today_models || {})
    .sort((a, b) => b[1] - a[1]).slice(0, 4)
    .map(([m, t]) => `<span class="badge ext">${esc(m)} ${fmt(t)}</span>`).join("");
  // 客户端预算（/api/keys/info 的 budget 块：仅配置 limit 的 key 才曝光）。
  // tokens / cost 双 usage bar：超 80% 琥珀、触顶红；无限额 key 不显示条。
  let budgetHtml = "";
  const b = k.budget;
  if (b) {
    const tLimit = b.limit_tokens, tUsed = Number(b.used_tokens || 0);
    const cLimit = b.limit_cost, cUsed = Number(b.used_cost || 0);
    const barCls = (l, u) => (u / l >= 1 ? "crit" : (u / l > 0.8 ? "warn" : "ok"));
    const tPct = tLimit ? Math.min(100, tUsed / tLimit * 100) : 0;
    const cPct = cLimit ? Math.min(100, cUsed / cLimit * 100) : 0;
    if (tLimit != null) {
      budgetHtml += `<div class="acc-meta"><span>预算 tokens <b>${fmt(tUsed)} / ${fmt(tLimit)}</b></span><span>在途 <b>${b.inflight || 0}</b></span></div>`
        + `<div class="exp-bar"><i class="${barCls(tLimit, tUsed)}" style="width:${tPct}%"></i></div>`;
    }
    if (cLimit != null) {
      budgetHtml += `<div class="acc-meta"><span>预算 cost <b>$${fmtSpend(cUsed)} / $${fmtSpend(cLimit)}</b></span><span>重置 ${esc((b.reset_at || "—").slice(5, 16))}</span></div>`
        + `<div class="exp-bar"><i class="${barCls(cLimit, cUsed)}" style="width:${cPct}%"></i></div>`;
    }
  }
  return `<div class="acc-card">
    <div class="acc-top"><span class="acc-name">${esc(k.label)}</span>${quotaBadge}</div>
    <div class="acc-id">${esc(k.key_preview || "")}</div>
    <div class="acc-meta"><span>今日 ${fmt(used)} tokens</span><b>${k.today_requests || 0} 次</b></div>
    ${models ? `<div class="acc-meta">${models}</div>` : ""}
    ${limit ? `<div class="hint">限额 ${fmt(limit)}/天 · 剩余 ${fmt(Math.max(0, limit - used))}</div>` : ""}
    ${budgetHtml}
  </div>`;
}

async function loadKeysInfo() {
  if (!isAdmin || !keysSupported) return;
  try {
    const r = await api("/api/keys/info");
    if (r.status === 404) { keysSupported = false; return; }
    const d = await r.json();
    const keys = d.keys || [];
    if (!keys.length) return; // 未配置 key，不显示面板
    const sec = ensureKeysPanel();
    if (!sec) return;
    $("keys-tag").textContent = `${keys.length} KEYS`;
    $("keys-grid").innerHTML = keys.map(renderKeyCard).join("");
  } catch (e) { /* 忽略瞬时错误 */ }
}

// ─── 运维面板：排队车道 / 会话粘性 / 账号质量 / 语料采集 ──────────────

// 排队车道（GET /api/gate，3s 轮询；老后端无此接口时静默降级）
let gateSupported = true;

function renderGate(d) {
  const fc = d.free_channel || {};
  const busy = !!fc.busy;
  const waiting = Number(fc.waiting || 0);
  $("gate-led").className = busy ? "led" : "led on"; // 空闲绿 / 占用琥珀
  $("gate-state-text").textContent = busy ? "占用" : "空闲";
  $("gate-waiting").textContent = waiting;
  $("gate-count-tag").textContent = busy ? (waiting > 0 ? `BUSY +${waiting}` : "BUSY") : "IDLE";
}

async function loadGate() {
  if (!isAdmin || !gateSupported) return;
  try {
    const r = await api("/api/gate");
    if (r.status === 404) { gateSupported = false; return; }
    renderGate(await r.json());
  } catch (e) { /* 忽略瞬时错误 */ }
}

// 会话粘性（GET /api/affinity/stats，15s 轮询）
let affinitySupported = true;

// why 码中文标签（proxy_core/affinity.py WHY_* 全集，语义以
// pick_prefer / _account_state 产出路径为准）
const AFFINITY_WHY = {
  "kept": "保持粘连（账号正常）",
  "turn": "工具循环内（同 turn 续请求）",
  "session": "会话级粘性（策略档）",
  "cache": "prompt cache 命中（跨 turn 粘）",
  "new-turn": "新 turn（策略档）",
  "no-cache": "缓存证据不足（<1024 tok）",
  "no-data": "上游无 cache 字段",
  "cold": "冷启动（无记忆/已过期）",
  "resting": "账号冷却中",
  "spent": "配额耗尽（402）",
  "disabled": "账号已禁用",
  "off": "粘性开关关闭",
};

function renderAffinity(d) {
  const stuck = Number(d.stuck_sessions || 0);
  const dist = d.why_distribution || {};
  const has = stuck > 0 || Object.keys(dist).length > 0;
  $("affinity-live").style.display = has ? "" : "none";
  $("affinity-empty").style.display = has ? "none" : "";
  $("affinity-count-tag").textContent = has ? `${stuck} STUCK` : "AFFINITY";
  if (!has) return;
  $("affinity-count").textContent = stuck;
  $("affinity-why").innerHTML = Object.entries(dist)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<div class="dist-row row"><span class="dist-name" title="${esc(k)}">${esc(AFFINITY_WHY[k] || k)}</span><span class="dist-val">${v}</span></div>`)
    .join("");
}

async function loadAffinity() {
  if (!isAdmin || !affinitySupported) return;
  try {
    const r = await api("/api/affinity/stats");
    if (r.status === 404) { affinitySupported = false; return; }
    renderAffinity(await r.json());
  } catch (e) { /* 忽略瞬时错误 */ }
}

// 账号质量（GET /api/stats/quality，30s 轮询）
let qualitySupported = true;

function fmtMs(v) { return v == null ? "—" : Math.round(Number(v)) + "ms"; }

function renderQuality(d) {
  // samples 降序、前 50 截断（后端 quality_aggregate 同口径，前端兜底再固化一次）
  const rows = (d.rows || [])
    .slice()
    .sort((a, b) => (Number(b.samples) || 0) - (Number(a.samples) || 0))
    .slice(0, 50);
  const wrap = $("quality-wrap"), empty = $("quality-empty");
  $("quality-count-tag").textContent = rows.length ? `${rows.length} ROWS` : "QUALITY";
  wrap.style.display = rows.length ? "" : "none";
  empty.style.display = rows.length ? "none" : "";
  if (!rows.length) return;
  $("quality-rows").innerHTML = rows.map(r => `<tr class="row">
      <td>${esc(r.user_id)}</td>
      <td>${esc(r.model)}</td>
      <td>${fmtMs(r.ttft_p50)}</td>
      <td>${fmtMs(r.ttft_text_p50)}</td>
      <td>${r.tokens_per_sec == null ? "—" : Number(r.tokens_per_sec).toFixed(1)}</td>
      <td>${r.samples == null ? "—" : r.samples}</td>
      <td>${r.water_hits == null ? "—" : r.water_hits}</td>
    </tr>`).join("");
}

async function loadQuality() {
  if (!isAdmin || !qualitySupported) return;
  try {
    const r = await api("/api/stats/quality");
    if (r.status === 404) { qualitySupported = false; return; }
    renderQuality(await r.json());
  } catch (e) { /* 忽略瞬时错误 */ }
}

// 语料采集（GET /api/corpus/stats，30s 轮询）
let corpusSupported = true;

function fmtTop3(obj) {
  const top = Object.entries(obj || {})
    .filter(([, v]) => Number(v) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, 3);
  return top.map(([k, v]) => `${k} ${v}`).join(" · ") || "—";
}

function renderCorpus(d) {
  const on = !!d.enabled;
  $("corpus-off").style.display = on ? "none" : "";
  $("corpus-on").style.display = on ? "" : "none";
  $("corpus-count-tag").textContent = on ? "ON" : "OFF";
  if (!on) return;
  const t = d.today || {}, m = d.month || {};
  $("corpus-today-count").textContent = t.count || 0;
  $("corpus-today-tokens").textContent = fmt(t.tokens || 0);
  $("corpus-month-count").textContent = m.count || 0;
  $("corpus-month-tokens").textContent = fmt(m.tokens || 0);
  $("corpus-models").textContent = fmtTop3(d.by_model);
  $("corpus-roles").textContent = fmtTop3(d.by_role);
  $("corpus-water").textContent = d.water_suspect || 0;
  $("corpus-redact").textContent = d.redact_hits || 0;
  $("corpus-discard").textContent = d.discarded || 0;
  $("corpus-backlog").textContent = d.backlog || 0;
}

async function loadCorpus() {
  if (!isAdmin || !corpusSupported) return;
  try {
    const r = await api("/api/corpus/stats");
    if (r.status === 404) { corpusSupported = false; return; }
    renderCorpus(await r.json());
  } catch (e) { /* 忽略瞬时错误 */ }
}

// ─── 统计与图表 ──────────────────────────────────────────────


let distMode = "model";
let lastDetails = [];
let trendHours = 24;

async function loadTokenStats() {
  if (!isAdmin) return;
  try {
    const r = await api("/api/token-stats");
    const d = await r.json();
    const s = d.summary || {};
    lastDetails = d.details || [];
    animateNum($("kpi-requests"), s.requests || 0, fmt);
    animateNum($("kpi-tokens"), s.total_tokens || 0, fmt);
    $("kpi-req-sub").textContent = `IN ${fmt(s.prompt_tokens)} / OUT ${fmt(s.completion_tokens)}`;
    $("kpi-tok-sub").textContent = `${lastDetails.length} 组账号×模型`;
    renderDist();
  } catch (e) { /* 忽略 */ }
}

async function loadHistory() {
  if (!isAdmin) return;
  try {
    const r = await api(`/api/stats/history?hours=${trendHours}`);
    const d = await r.json();
    drawTrend($("trend-canvas"), d.history || []);
  } catch (e) { /* 忽略 */ }
}

// 趋势图：双 Token 面积线 + 请求数柱（右轴）
function drawTrend(cv, rows) {
  const dpr = devicePixelRatio || 1;
  const W = cv.clientWidth, H = 180;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const padL = 46, padR = 40, padT = 12, padB = 22;
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = Math.max(rows.length, 2);
  const maxTok = Math.max(1, ...rows.map(r => r.total_tokens));
  const maxReq = Math.max(1, ...rows.map(r => r.requests));
  const x = (i) => padL + (i / (n - 1)) * iw;
  const yT = (v) => padT + ih - (v / maxTok) * ih;
  const yR = (v) => padT + ih - (v / maxReq) * ih;

  // 网格
  ctx.strokeStyle = "rgba(0,229,255,0.08)";
  ctx.fillStyle = "rgba(95,116,140,0.9)";
  ctx.font = "9px Consolas, monospace";
  ctx.textAlign = "right";
  for (let g = 0; g <= 4; g++) {
    const y = padT + (g / 4) * ih;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillText(fmt(Math.round(maxTok * (1 - g / 4))), padL - 6, y + 3);
    ctx.textAlign = "left";
    ctx.fillText(String(Math.round(maxReq * (1 - g / 4))), W - padR + 6, y + 3);
    ctx.textAlign = "right";
  }
  // X 轴标签（最多 8 个）
  ctx.textAlign = "center";
  const step = Math.max(1, Math.ceil(n / 8));
  rows.forEach((r, i) => {
    if (i % step === 0) ctx.fillText(r.label, x(i), H - 6);
  });

  // 请求数柱（绿色，低透明）
  const bw = Math.max(2, Math.min(14, iw / n * 0.5));
  ctx.fillStyle = "rgba(0,255,163,0.18)";
  rows.forEach((r, i) => {
    if (r.requests > 0) ctx.fillRect(x(i) - bw / 2, yR(r.requests), bw, padT + ih - yR(r.requests));
  });

  // 面积线
  function areaLine(key, color, glow) {
    ctx.beginPath();
    rows.forEach((r, i) => i === 0 ? ctx.moveTo(x(i), yT(r[key])) : ctx.lineTo(x(i), yT(r[key])));
    ctx.strokeStyle = color; ctx.lineWidth = 1.8;
    ctx.shadowColor = glow; ctx.shadowBlur = 8;
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.lineTo(x(rows.length - 1), padT + ih); ctx.lineTo(x(0), padT + ih); ctx.closePath();
    const g = ctx.createLinearGradient(0, padT, 0, padT + ih);
    g.addColorStop(0, color.replace(")", ",0.22)").replace("rgb", "rgba"));
    g.addColorStop(1, color.replace(")", ",0)").replace("rgb", "rgba"));
    ctx.fillStyle = g;
    ctx.fill();
  }
  if (rows.length) {
    areaLine("prompt_tokens", "rgb(0,229,255)", "#00e5ff");
    areaLine("completion_tokens", "rgb(236,111,217)", "#ec6fd9");
  } else {
    ctx.fillStyle = "rgba(95,116,140,0.8)";
    ctx.textAlign = "center";
    ctx.font = "11px Consolas, monospace";
    ctx.fillText("暂无用量数据 — 发起一次请求试试", W / 2, H / 2);
  }
}

// 分布：环形图 + 排行条
function renderDist() {
  const agg = {};
  for (const r of lastDetails) {
    const k = distMode === "model" ? (r.model || "未知") : (r.user_id || "未知");
    agg[k] = (agg[k] || 0) + r.total_tokens;
  }
  const entries = Object.entries(agg).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const total = entries.reduce((s, e) => s + e[1], 0) || 1;
  const colors = ["#00e5ff", "#ec6fd9", "#00ffa3", "#ffb800", "#8f86e8", "#f0856a"];

  const cv = $("dist-canvas");
  const dpr = devicePixelRatio || 1;
  const W = cv.clientWidth, H = 200;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 18, r2 = R * 0.62;
  let ang = -Math.PI / 2;
  if (!entries.length) {
    ctx.fillStyle = "rgba(95,116,140,0.8)";
    ctx.font = "11px Consolas, monospace";
    ctx.textAlign = "center";
    ctx.fillText("暂无数据", cx, cy);
  }
  entries.forEach(([k, v], i) => {
    const sweep = (v / total) * Math.PI * 2;
    ctx.beginPath();
    ctx.arc(cx, cy, R, ang, ang + sweep);
    ctx.arc(cx, cy, r2, ang + sweep, ang, true);
    ctx.closePath();
    ctx.fillStyle = colors[i % colors.length] + "cc";
    ctx.shadowColor = colors[i % colors.length];
    ctx.shadowBlur = 10;
    ctx.fill();
    ctx.shadowBlur = 0;
    ang += sweep;
  });
  if (entries.length) {
    ctx.fillStyle = "#fff";
    ctx.font = "700 16px Consolas, monospace";
    ctx.textAlign = "center";
    ctx.fillText(fmt(total), cx, cy + 2);
    ctx.fillStyle = "rgba(95,116,140,1)";
    ctx.font = "9px Consolas, monospace";
    ctx.fillText("TOTAL TOKENS", cx, cy + 16);
  }

  $("dist-list").innerHTML = entries.map(([k, v], i) => `
    <div class="dist-row">
      <span class="dist-name" style="color:${colors[i % colors.length]}">${esc(k)}</span>
      <span class="dist-bar"><i style="width:${(v / total * 100).toFixed(1)}%"></i></span>
      <span class="dist-val">${fmt(v)} · ${(v / total * 100).toFixed(1)}%</span>
    </div>`).join("");
}

async function resetTokenStats() {
  if (!confirm("确认清空所有 Token 用量统计？此操作不可撤销。")) return;
  await api("/api/token-stats/reset", { method: "POST" });
  toast("统计已清空");
  loadTokenStats(); loadHistory();
}

// ─── 实时动态 ────────────────────────────────────────────────

let activityPaused = false;
let activityMaxSeq = 0;

function toggleActivityPause(btn) {
  activityPaused = !activityPaused;
  btn.textContent = activityPaused ? "继续" : "暂停";
}

// feed 着色映射：status 类型 gate=青 / affinity=品红（proxy.py 的
// activity.add 以 status 字段携带，kind 仅为 info/warn）；kind=ok
// 绿；其余=红（错误）
function feedCls(e) {
  if (e.status === "gate") return "gate";
  if (e.status === "affinity") return "affinity";
  if (e.kind === "ok") return "ok";
  return "error";
}

async function loadActivity() {
  if (!isAdmin || activityPaused) return;
  try {
    const r = await api("/api/activity");
    const d = await r.json();
    const events = d.events || [];
    const feed = $("activity-feed");
    if (!events.length) {
      feed.innerHTML = '<div class="feed-empty">等待请求…</div>';
      return;
    }
    activityMaxSeq = events[0].seq;
    feed.innerHTML = events.map(e => `
      <div class="feed-item ${feedCls(e)}">
        <span class="feed-time">${esc(e.time)}</span>
        <span class="feed-msg" title="${esc(e.message)}">${esc(e.message)}</span>
        <span class="feed-meta"><b>${esc(e.latency_ms)}</b>ms · <b>${fmt(e.tokens)}</b>tok</span>
      </div>`).join("");
  } catch (e) { /* 忽略 */ }
}

// ─── 日志终端 ────────────────────────────────────────────────

let logPaused = false;
let logFilter = "all";

function toggleLogPause(btn) {
  logPaused = !logPaused;
  btn.textContent = logPaused ? "继续" : "暂停";
}

async function loadLogs(manual = false) {
  if (!isAdmin || (logPaused && !manual)) return;
  try {
    const r = await api("/api/logs");
    const d = await r.json();
    const term = $("log-terminal");
    const lines = (d.logs || []).filter(l => {
      if (logFilter === "all") return true;
      return l.includes(`[${logFilter}]`);
    });
    if (!lines.length) {
      term.innerHTML = '<span class="t-dim">// 无匹配日志</span>';
      return;
    }
    term.innerHTML = lines.map(l => {
      let cls = "t-info";
      if (l.includes("[WARNING]")) cls = "t-warning";
      else if (l.includes("[ERROR]")) cls = "t-error";
      return `<span class="t-line ${cls}">${esc(l)}</span>`;
    }).join("");
    term.scrollTop = term.scrollHeight;
  } catch (e) { /* 忽略 */ }
}

// ─── 接入代码片段 ────────────────────────────────────────────

let snippetLang = "curl";

function snippetText(lang) {
  const base = $("endpoint-url").textContent || window.location.origin;
  if (lang === "curl") {
    return `curl ${base}/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer sk-anything" ^
  -d "{\\"model\\":\\"GLM-5.3\\",\\"messages\\":[{\\"role\\":\\"user\\",\\"content\\":\\"你好\\"}]}"`;
  }
  if (lang === "python") {
    return `from openai import OpenAI

client = OpenAI(
    base_url="${base}/v1",
    api_key="sk-anything",   # 任意值，代理自动轮换账号
)

resp = client.chat.completions.create(
    model="GLM-5.3",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)`;
  }
  return `const resp = await fetch("${base}/v1/chat/completions", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": "Bearer sk-anything",
  },
  body: JSON.stringify({
    model: "GLM-5.3",
    messages: [{ role: "user", content: "你好" }],
  }),
});
const data = await resp.json();
console.log(data.choices[0].message.content);`;
}

function renderSnippet() {
  $("snippet-box").textContent = snippetText(snippetLang);
}

function copySnippet(btn) {
  copyText(snippetText(snippetLang), btn);
}

// ─── API 测试台 ──────────────────────────────────────────────

let pgBusy = false;

async function loadModels() {
  try {
    const r = await fetch("/qidi/models", { headers: apiAuthHeaders() });
    const d = await r.json();
    const ids = (d.data || []).map(m => m.id).filter(Boolean);
    const sel = $("pg-model");
    if (!ids.length) { sel.innerHTML = '<option value="">无可用模型</option>'; return; }
    sel.innerHTML = ids.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join("");
    const prefer = ids.find(id => /flash/i.test(id)) || ids[0];
    sel.value = prefer;
  } catch (e) {
    $("pg-model").innerHTML = '<option value="">模型加载失败</option>';
  }
}

// ─── API Key（服务端启用鉴权时测试台需要）────────────────────

function apiKey() {
  const el = $("pg-apikey");
  if (!el) return "";
  return el.value.trim();
}

function apiAuthHeaders() {
  const k = apiKey();
  return k ? { "Authorization": `Bearer ${k}` } : {};
}

function persistApiKey() {
  const el = $("pg-apikey");
  if (!el) return;
  const saved = localStorage.getItem("pg_api_key") || "";
  if (saved) el.value = saved;
  el.addEventListener("input", () => localStorage.setItem("pg_api_key", el.value.trim()));
}
persistApiKey();

async function pgSend() {
  if (pgBusy) return;
  const model = $("pg-model").value;
  const input = $("pg-input").value.trim();
  const stream = $("pg-stream").checked;
  const out = $("pg-output"), meta = $("pg-meta");
  if (!model) { toast("请先选择模型", true); return; }
  if (!input) { toast("请输入测试消息", true); return; }

  pgBusy = true;
  $("pg-send").disabled = true;
  out.textContent = "";
  out.classList.add("streaming");
  meta.textContent = "请求中…";
  const t0 = performance.now();

  try {
    const r = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...apiAuthHeaders() },
      body: JSON.stringify({
        model,
        messages: [{ role: "user", content: input }],
        stream,
      }),
    });

    if (!r.ok) {
      const txt = await r.text();
      out.textContent = `✗ HTTP ${r.status}\n${txt}`;
      meta.textContent = "";
      return;
    }

    if (stream) {
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "", text = "", usage = null;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6);
          if (payload === "[DONE]") continue;
          try {
            const j = JSON.parse(payload);
            const delta = j.choices?.[0]?.delta?.content || j.choices?.[0]?.message?.content || "";
            if (delta) { text += delta; out.textContent = text; out.scrollTop = out.scrollHeight; }
            if (j.usage) usage = j.usage;
          } catch (e) { /* 半行 JSON 忽略 */ }
        }
      }
      const ms = Math.round(performance.now() - t0);
      meta.innerHTML = `耗时 <b>${ms}ms</b>` + (usage ? ` · 输入 <b>${fmt(usage.prompt_tokens)}</b> · 输出 <b>${fmt(usage.completion_tokens)}</b>` : "");
    } else {
      const j = await r.json();
      const text = j.choices?.[0]?.message?.content || JSON.stringify(j, null, 2);
      out.textContent = text;
      const ms = Math.round(performance.now() - t0);
      meta.innerHTML = `耗时 <b>${ms}ms</b>` + (j.usage ? ` · 输入 <b>${fmt(j.usage.prompt_tokens)}</b> · 输出 <b>${fmt(j.usage.completion_tokens)}</b>` : "");
    }
  } catch (e) {
    out.textContent = `✗ 请求失败: ${e.message}`;
    meta.textContent = "";
  } finally {
    out.classList.remove("streaming");
    $("pg-send").disabled = false;
    pgBusy = false;
    loadTokenStats(); loadHistory(); loadActivity();
  }
}

$("pg-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); pgSend(); }
});

// ─── 配置 ────────────────────────────────────────────────────

async function saveConfig() {
  const newPwd = $("newPwd").value;
  if (!newPwd) { toast("请输入新密码", true); return; }
  await api("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ admin_password: newPwd }),
  });
  closeModal("configModal");
  $("newPwd").value = "";
  toast("密码已设置，下次访问需登录");
  loadAuthState();
}

async function clearPassword() {
  if (!confirm("确认清除管理密码？局域网内任何人都可直接管理。")) return;
  await api("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ admin_password: "" }),
  });
  toast("密码已清除");
  loadAuthState();
}

async function loadAuthState() {
  try {
    const r = await api("/api/config");
    const d = await r.json();
    $("auth-state").textContent = d.admin_password
      ? "已设置管理密码（SHA256 哈希存储）"
      : "未设置管理密码（局域网开放访问）";
  } catch (e) { /* 忽略 */ }
}

// ─── 弹窗 ────────────────────────────────────────────────────

function openModal(id) { $(id).style.display = "block"; }
function closeModal(id) { $(id).style.display = "none"; }
document.querySelectorAll(".modal").forEach(m => {
  m.addEventListener("click", (e) => { if (e.target === m) m.style.display = "none"; });
});

// ─── 事件绑定：选项卡 ────────────────────────────────────────

document.querySelectorAll(".snippet-tabs .stab").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".snippet-tabs .stab").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    snippetLang = b.dataset.snippet;
    renderSnippet();
  });
});

document.querySelectorAll("[data-hours]").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("[data-hours]").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    trendHours = Number(b.dataset.hours);
    loadHistory();
  });
});

document.querySelectorAll("[data-dist]").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("[data-dist]").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    distMode = b.dataset.dist;
    renderDist();
  });
});

document.querySelectorAll("[data-logfilter]").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("[data-logfilter]").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    logFilter = b.dataset.logfilter;
    loadLogs(true);
  });
});

// ─── 启动与轮询 ──────────────────────────────────────────────

function loadAll() {
  loadAccounts();
  loadUpstream();
  loadProvidersInfo();
  loadKeysInfo();
  loadInflight();
  loadWater();
  loadTokenStats();
  loadHistory();
  loadActivity();
  loadLogs(true);
  loadAuthState();
  loadModels();
  loadGate();
  loadAffinity();
  loadQuality();
  loadCorpus();
}

loadHealth();
renderSnippet();
checkAuth();

setInterval(loadHealth, 5000);
setInterval(() => { if (isAdmin) { loadActivity(); } }, 4000);
setInterval(() => { if (isAdmin) { loadAccounts(); loadTokenStats(); loadHistory(); loadAffinity(); } }, 15000);
setInterval(() => { if (isAdmin) { loadLogs(); } }, 10000);
setInterval(() => { if (isAdmin) { loadUpstream(); loadProvidersInfo(); loadKeysInfo(); } }, 60000);
setInterval(() => { if (isAdmin) { loadKeysInfo(); loadQuality(); loadCorpus(); } }, 30000);
setInterval(() => { if (isAdmin) { loadGate(); } }, 3000);
setInterval(() => { if (isAdmin) { loadInflight(); } }, 3000);
setInterval(() => { if (isAdmin) { loadWater(); } }, 15000);
addEventListener("resize", () => { loadHistory(); renderDist(); });
