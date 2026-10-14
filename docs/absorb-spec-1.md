# 吸收批次实施规格 v2 吸收-1/2（R-A~R-E）

> 依据：docs/competitor-analysis-cliproxyapi.md（第一梯队五项）
> 修订记录：v2（2026-10-14）——双席审计（GLM-5.3-222 REJECT + KIMI-K3-222 代 Step-5 REJECT）返工闭环：修正 R-B 构造点归属错位、R-C 心跳机制选型与双桥透传、R-D 现状语义反转、R-A 原子写参照与路径常量纪律、R-E 双白名单透传。
> v3（2026-10-14）——第三席复审（OpenCode/step-5-preview-free 经 ai-bridge，ACCEPT WITH NITS）返工：F-1 record 准入条件、F-2 inband_error 载荷改造、F-3 429/404 判据来源标注、F-4 admin 触点表归属、F-7~F-9/F-11~F-14 次要项全部落实。
> 基线标注（v3 修正）：行号基于本地工作副本 HEAD=ad4a5b6（= 远端 c9c6bf4 + 2b7d68a[F14+T13] + ad4a5b6[assets]；README/config.example 有未提交改动、docs/ 未跟踪，不影响源码行号）；实施前以当时 HEAD 复核（R-006）。

## R-A 冷却状态持久化

### 背景与痛点（v2 修正）
- routes/proxy.py:359-360 `PROVIDER_QUOTA_COOLDOWN_S = 900.0` + `_prov_quota_cooldown = {}`（provider→until_ts）：外部静态 key provider 全 key 429 后整渠道冷却 900s，**纯内存，重启即丢**。
- 触点全表（v2 补）：读检查 proxy.py:2649；set：proxy.py:2836 / 2902；clear（任一 key 成功即解除）：proxy.py:2843 / 2907。
- 仓库共三套冷却，本项范围声明（v2 新增）：
  1. 主账号池（accounts.mark_cooldown）——**已持久化**（accounts.py:100-136 内 `if cooled: self._save()`，审计已核实，悬案关闭）
  2. 静态 key provider（`_prov_quota_cooldown`）——**本项目标**
  3. cline/minicode 通道级冷却（cline.py:155/300-308 `self.cooldown_until`，429 日配额冷却上限 6h；minicode.py:177）——**一并纳入**（比 900s 更痛：重启丢 6h 冷却），store 加 `channels` 节；凭证刷新语义由通道自身负责，store 只保 `until_ts`

### 设计
1. 路径常量（v2 修正 §7.7 纪律）：`settings.py` 新增 `COOLDOWNS_FILE`；`tests/conftest.py` 重定向表同步追加。
2. 新模块 `proxy_core/cooldown_store.py`：
   - 结构 `{version: 1, saved_at, providers: {name: until_ts}, channels: {prov|label: until_ts}}`
   - API：`persist()` / `load()`（加载丢弃过期条目）
   - 原子写（v2 修正）：**与 affinity._save_to_disk 同款**（affinity.py:385-410：pid 后缀 tmp + os.replace，防双实例共写交错）——**不学 stats._save 的非原子直写**
   - 后台模式（v2 修正；v3 补 F-12）：**复用 affinity 扫尾模式**（daemon 线程周期扫尾，affinity.py:432-460 同款），不另起 30s 防抖 worker、不用 atexit（消除退出竞争）；dirty 标记驱动。**不沿袭 affinity 的丢窗**：affinity `_worker` 在 `_save_to_disk()` 失败后仍无条件清 dirty（仅 log.warning，下次不重试）——cooldown_store 的 sweep 改为**持久化失败时保留 dirty**（下轮重试）+ warning 日志（R-004 失败可见），并补"persist 失败可见"用例
   - 加载时机（v2 修正）：**懒加载**（首次访问 `_ensure_loaded_locked`，affinity.py:336-343 同款），禁止模块导入期触盘
3. routes/proxy.py：5 触点挂 store（读点预热 + set×2 + clear×2）；cline/minicode 冷却实例属性在构造/恢复处挂接 channels 节。**channels 回灌时点（v3 新增，第三席 F-13）**：cline/minicode 通道实例由 `reload_channels_from_config`（providers.py:181-184）在 config mtime 变化时重建、冷却清零——定义回灌时点为**重载完成后**按 `prov|label` 键从 store 恢复 `until_ts` 到新实例（晚于实例构造、早于首个请求）；label 不稳定（重命名/删除）的键过期自然失效，不跨实例错挂；恢复失败仅 warning 不阻断重载。
4. `.gitignore` + `cooldowns.json`；config：`cooldown.persist`（默认 true——防撞墙是存在理由；false 时不读不写）。

### 测试（tests/test_cooldown_store.py）
1. persist→load 往返；load 丢弃过期
2. 原子替换中断不破坏旧文件；**双实例 pid-tmp 并存**（R-009 补）
3. JSON 损坏 → 空 dict + warning 日志（R-004）
4. **clear 路径**：成功解除 → persist → 重载后不再拦截（状态保真回归，审计 M-11）
5. **persist=false 分支**：不读不写（R-010 补）
6. channels 节：cline 冷却恢复
7. 路径隔离：conftest 重定向下测试不写真实仓库根

## R-B 每 Provider 代理出口

### 背景与痛点（v2 重写）
- 痛点实证：openai-next 按家庭宽带 IP 403(1010)，此前只能整组改走云端 relay。
- **真实构造点全表（v2 修正，v1 的 L273/452/1397 全部属于主账号池，作废）**：
  | 路径 | 位置 | 服务对象 |
  |---|---|---|
  | `_forward_external._open_stream` | proxy.py:2718 | 静态 key/atomcode 流式 |
  | `_forward_external` 非流式 | proxy.py:2862 附近（以 HEAD 复核） | 静态 key/atomcode 非流式 |
  | cline 流式/非流式 | proxy.py:1859 / 2004 | Cline 通道 |
  | minicode 流式/非流式 | proxy.py:2206 / 2350 | MiniCode 通道 |
  | admin 探测/模型拉取 | admin.py:1397/1413/2058 | 面板探针（直连外部 provider base_url） |
  | water 金丝雀 | water.py:308 | 注水探针 |
- 范围声明（v2 新增；v3 修正 F-4）：**静态 key/atomcode + cline + minicode 转发路径必做**；admin 探测与 water 探针**同样接入**（否则面板探测 403 而转发成功的自相矛盾 UX）；**admin.py:2168（主池 LITELLM_API_BASE /v1/models）与 admin.py:1705/1882（主池探针）不属外部 provider 路径、显式排除**（主池探针如需代理由全局 requests.proxy_url 兜底，单列说明）；embeddings 端点与 opencode 桥（127.0.0.1 本机）显式排除。

### 设计
1. config：`providers.<name>.proxy_url`（http://、https://、socks5://；空=直连；`direct`=显式直连覆盖全局）；全局 `requests.proxy_url` 默认空。
2. providers.py：解析进 prov dict；**失效纪律（v2 修正）**：跟随 providers 的 **config mtime 即时失效缓存**（providers.py:174-178 `_cache=(mtime,...)`），改 proxy_url 即时重建并关闭旧 Client——**不是** `_provider_cache` 120s TTL（那在 admin.py:1852 是面板展示缓存，与本路无关）。
3. **共享 Client 生命周期（v2 新增，阻断项修复；v3 补 F-11）**：`_clients: {(name, proxy_url): Client}` 缓存复用；`_UpstreamOpen.close()`（proxy.py:421-435）语义改造——共享 Client **不随流关闭**（close 只关 resp；client 归缓存所有，失效时统一 close）。**旧 Client 回收（v3 新增）**：providers `_load_providers` mtime 重建时，对缓存中被移除键（proxy_url 变更/provider 删除）的旧 Client 执行 close 并从 `_clients` 淘汰，防止泄漏。回归测试覆盖流结束后同 provider 后续请求正常 + mtime 变更后旧 Client 已关闭。
4. 代理参数：统一 `proxy=`（httpx 0.26+ 即支持，本库 pin >=0.27）；socks 依赖 `httpx[socks]`。
5. **部署面（v2 修正）**：requirements.txt + **启动.bat + deploy.sh** 安装行同步改 `httpx[socks]`（两安装器现存 `pip install flask httpx` 与 import 检查，不改则存量机器运行时缺 socksio）。
6. **trust_env（v2 新增）**：所有按 provider 构造的 Client 一律 `trust_env=False`（不吃 HTTP(S)_PROXY 环境变量；`direct` 语义才成立；库内先例 opencode.py:248）。优先级链：per-provider > 全局 config > 无。
7. 不可达代理：走现有失败路径；**非流式路径同步收敛 connect 超时**（现 timeout=300 标量，配坏代理最长挂 5min——收敛 connect=15 与流式一致）。

### 测试（tests/test_provider_proxy.py）
1. 解析四态（http/socks5/direct/缺省）+ 全局覆盖优先级
2. Client 按 (name, proxy_url) 隔离；流结束后缓存 Client 存活可用
3. trust_env=False 断言（环境代理变量不生效）
4. 坏 proxy_url 走正常错误返回不崩；mtime 变更后 Client 重建
5. admin 探测/water 探针路径的代理透传（范围表一致性）

### 风险与回滚
- socksio 新依赖（安装器三处同步）；回滚：proxy_url 不配即零行为变化

## R-C SSE 心跳保活

### 背景与痛点
- 上游静默期（思考型模型 prefill）无字节下发，中间层/客户端可能掐连接；GATE_NO_EVENT_TIMEOUT=20s（proxy.py:372）是 peek 门内部死限，非客户端心跳。

### 设计（v2 重写）
1. **实现机制（v2 新增，阻断项修复）**：阻塞迭代 `for chunk in stream_iter`（proxy.py:875-877）下"距上次 chunk 超时 yield 心跳"不可行（httpx ReadTimeout 致命且不可续读）。采用**后台读线程 + Queue**：
   - 每流起一个读线程：`q.put(next(stream_iter))`，异常经队列传回走现有 except 分支
   - 主生成器 `q.get(timeout=keepalive_seconds)`，Queue.Empty → yield `: keepalive\n\n` 后继续等同一队列（同一上游连接不中断）
   - 与 prefetched 消费、peek_iter 续读、usage/TTFT 记账的协同点逐项过（实施时列出）
   - GeneratorExit/客户端断开：通知读线程退出（关闭 resp 触发读异常）——补清理测试
2. **协议桥透传（v2 新增，阻断项修复）**：/v1/messages 桥（proxy.py:3440 `if not line.startswith("data:") ... continue`）与 /v1/responses 桥（proxy.py:3250）会吞掉注释行——两桥对 `:` 开头注释行**原样透传**，心跳对主力客户端（Claude Code / OpenClaw）生效。改动点表补两桥。
3. 配置：`requests.keepalive_seconds`（**默认 15s ON**；v2 声明：此为对"新默认保守=现状"原则的显式偏离，理由同 R-A——心跳的存在理由就是防掐线，默认关则功能无意义；0=可关）。
4. 心跳不重置 GATE_NO_EVENT_TIMEOUT（正交设计保留）。
5. 观测（v2 修正）：心跳**不逐条进 ActivityLog**（deque maxlen=200 会被挤出真实动态）——用聚合计数器（每流心跳次数汇总进该请求的动态条目）。

### 测试（tests/test_stream_keepalive.py）
1. **真静默**：首 chunk 后上游完全静默 ≥3×keepalive → 断言 ≥2 条心跳 + 随后流能正常收尾/报错（v2 强化，防滴流假实现）
2. keepalive_seconds=0 关闭分支不发心跳
3. 正常快流不心跳；心跳不混入数据帧（内容逐字节分离）
4. 两桥注释行透传断言；GeneratorExit 清理（读线程退出无泄漏）

### 风险与回滚
- 读线程引入的并发面（每流一线程，异步框架无；上限=并发流数，可接受）；回滚 keepalive_seconds=0

## R-D 会话粘性 rebind 分级

### 背景与现状重勘（v2 重写）
- **现状真实语义（v1 声明"绑定后失败=解绑换号"失实，作废）**：
  - affinity.py **不存在解绑/清除绑定路径**（全文唯一写入口 record()，仅成功路径调用：流式 proxy.py:919 saw_done、非流式 proxy.py:1465-1467）
  - 失败时绑定原样保留；transient 失败（peek 判负"绝不 _penalize_account"，proxy.py:630-633）后下请求**粘回原号**
  - 401/402/403 触发 mark_cooldown → 冷却期间不粘（事实性临时解绑）→ **冷却到期后又粘回原号**
  - 即：现状 ≈ "成功者覆盖 + 冷却驱动不粘"，**不存在"任何失败解绑"**
- 需求：CPA 语义的 quota 档——transient 失败 failover 本次但保持原绑定（保 prompt cache），配额/认证错误持久清绑定。

### 设计（v2 重写）
1. 配置 `routing.rebind_on`：**默认档 = 现状零改动**（不引入任何新行为，即 v1 错称的 any 档删除）；`quota` 档 opt-in。
2. quota 档行为：
   - transient（见映射表）失败 → failover 本次、**绑定保持**、记统计
   - hard 失败 → **持久清绑定**（需**新增** affinity 清除 API——现状没有；API：`clear_binding(session_key)`）
   - **record 准入条件改造（v3 新增，第三席 F-1）**：现状是任一成功即 `record` 覆写绑定（流式 proxy.py:917-919 / 非流式 1464-1466），与"绑定保持"直接冲突。quota 档下改为：**仅当"命中绑定账号且成功"才 `record`**；failover 到非绑定账号成功时**不覆写绑定**（仅 `_bump_why`/计数）。接线点：proxy.py:917 / 1464 的 record 调用准入条件。默认档下准入条件零改动（现状语义）。
3. **失败面 × 分类完整映射表（v2 新增；v3 修正 F-2/F-3/F-7）**——新模块 proxy_core/classify_failure.py（仅 R-D 依赖，v1"与 R-A 共享"声明删除——R-A 无分类需求）：
   | 失败面 | 归类 | 依据/来源 |
   |---|---|---|
   | peek inband_error | **二次归类（需数据源改造，见下）**：429/401/402/403/404→hard；5xx/其余→transient | _gate_classify（proxy.py:594-618，现仅返回字符串 "error"） |
   | peek empty_stream | transient | proxy.py:691（v3 校准） |
   | peek budget_exhausted | hard（配额族） | proxy.py:694 |
   | peek no_content_timeout | transient | proxy.py:705 |
   | peek_error: ReadTimeout | transient（**CPA 源**） | proxy.py:727 |
   | peek_error: 连接重置/其它异常 | transient | 同上 |
   | OAuth invalid_grant（token 刷新失败，cline/minicode 语义） | hard（认证族） | **CPA 源**（v3 补行，v2 表缺） |
   | 流中途截断（saw_done=False） | transient | proxy.py:883-886 |
   | 非流式 401/402/403 | hard | 库内既有（FAILOVER_UPSTREAM_STATUSES，proxy.py:339 与 _penalize 语义） |
   | **非流式 429 / 404** | hard（**CPA 源新增判据**，v3 据第三席 F-3 如实标注——库内 429 现走"按 Retry-After 等待重发"（proxy.py:1404-1417）、404 不在 FAILOVER 集合，两既有路径与本分类并存不冲突） | proxy.py:339 对照 |
   | 非流式 5xx / 408 | transient（**408 为 CPA 源**，v3 补标注） | proxy.py:339 对照 |
   | _stream_open 连接失败 | transient | proxy.py:464 |
   - **inband_error 数据源改造（v3 新增，第三席 F-2）**：现状 `_gate_classify`（proxy.py:594-618）对 error 帧仅返回字符串、`_finish_bad`（proxy.py:653-659）固定返回 "inband_error" 且丢弃帧载荷——**分类器无数据可分**。必须改造返回签名：inband error 的 status/载荷随 reason 传出（如 `reason=("inband_error", status)`），改动点：proxy.py:594-618 / 653-659 / 686 三处，进"改动点表"。
   - 判据来源纪律（v3 修正 F-3）：401/402/403 与 5xx 锚定库内既有 FAILOVER/penalize 语义（proxy.py:339 `FAILOVER_UPSTREAM_STATUSES`、:416、:994-999）；**429/404/408/invalid_grant 为 CPA 源新增判据，逐行标注于上表"来源"列**（库内既有 429 等待重发路径与硬分类并存不冲突）。
4. 面板：affinity snapshot 端点补 rebind 计数（_bump_why 同款扩展）。

### 测试（tests/test_affinity_rebind.py）
1. **默认档：现状逐字节回归**——429/整单失败后绑定原样保留、粘回原号（断言口径=现状真实语义，v1 的"任何失败解绑"断言删除）
2. quota 档：transient 失败后 pick_prefer 返回原号；hard 后绑定清除
3. clear_binding API 单测；映射表逐行用例（inband_error 的 429/5xx 二次归类各一）
4. failover 成功后下一条命中原号（保持绑定语义）

### 风险与回滚
- 原号反复 transient → 每请求 failover 一次；冷却机制兜底；回滚=默认档

## R-E 凭证权重

### 背景与痛点
- providers.py pick_key（L281-307）：disabled 过滤 + 最低负载优先（registry.load_of + _key_rr 轮转起点）。无权重：主力号与试用号同等消耗。

### 设计
1. **解析层透传（v2 新增，阻断项修复）**：
   - providers.py `_parse_keys`（L77-115）是白名单重建（entry 仅 key/label/disabled）——**必须把 weight 加入白名单**，否则解析期即被丢弃、功能静默失效；钳制（负数→0=软停用，与 CPA 非正数=排除一致；超 1000→1000；字符串 `"2"`→int 容错，isinstance 防御式纪律同款）
   - accounts.py `_load`（L49-63）同白名单重建——**weight 加入白名单**（否则首次 _save 全量回写即抹掉）；add_account 入参与 get_all_status 面板展示同步。**主池选号分两步（v3 修正，第三席 F-9）**：现状 `get_next`（accounts.py:160-290）为 loads 感知+游标轮转+prefer，**无 weight 参与**——本批仅做透传/面板展示/防回写抹除，**主池选号权重生效延后**为独立小项（改 get_next 属另一场手术，避免本批范围膨胀）；本批 accounts 侧不使用"生效"措辞
2. 语义：weight 默认 1；**0=软停用**不参与选号（与 F14 disabled 并存时 disabled 优先，面板徽章区分"停用(F14)"与"零权重"）；**越界/非法值钳制（负数→0、超 1000→1000、字符串→int）时逐 key 发一次性 warning**（R-004 可见，v3 补）
3. 选择公式：`effective_load = load / weight`，取最低者。weight=1 时数学上严格等于现状；**tiebreak 与早退语义原样保留**（同 effective_load 时 _key_rr 轮转起点保公平）。**次序定义（v3 修正，第三席 F-8）**：`weight<=0` 过滤发生在 `len(keys)==1` 早退**之前**（providers.py:296-297）——单 key 且 weight=0 时返回 None（"软停用"优先于"单 key 直选"），与 F14 disabled 同语义；早退仅在过滤后的可用集上判断。测试 1 断言口径逐项覆盖。
4. 与既有"重复 key=更高选中权重"机制（providers.py:31-33/93-97）并存语义文档化（config.example 注释）。
5. config.example.json 补注释（权重=负载除数，非绝对比例）。

### 测试（tests/test_key_weight.py）
1. **weight 缺省=1 → 与现状逐位一致**（含 tiebreak 轮转口径；len==1 早退在过滤后可用集上）
2. weight=0 不参与；disabled 优先于 weight=0；**单 key 且 weight=0 → None（v3 补，第三席 F-8）**
3. 同负载下 weight=2 先于 weight=1；越界/字符串容错（钳制时发 warning 断言）
4. **accounts 侧**：weight 透传（_load 白名单，防 _save 回写抹除）+ 面板展示；不参与 get_next（v3 修正措辞，第三席 F-9）
5. _parse_keys 对 weight 的白名单透传单测

### 风险与回滚
- 回滚：weight 不配即现状

## 实施顺序与依赖（v2 修正）
- R-A / R-B / R-C / R-E 相互独立可并行；**R-D 独立**（classify_failure 仅 R-D 用，删除 v1 的 R-A 共享依赖声明）
- **config 命名空间说明（v3 补，第三席 F-10）**：既有开关统一在 `features.*`（stream_gate/affinity/budget，proxy.py:489-524）；本批新开关按 CPA 语义分组落在 `requests.keepalive_seconds` / `cooldown.persist` / `routing.rebind_on`——**保持此命名（与竞品语义对齐、便于对照 CPA 文档）**，在 config.example.json 注明与 features.* 的命名约定差异及与既有 `routing.economy` 子树（router.py:109-123）的共存关系
- 每项独立提交，前缀 `吸收-<项>`；开工先跑基线（pytest 全绿 + node --check，当前远端基线待复核）

## 验收门
1. pytest 全绿（新基线 + 五个新测试文件）；node --check app.js
2. 桌面部署冒烟：重启后 provider/cline 冷却保持且**已解除的不复活**（R-A）；openai-next 配代理对比（R-B）；Claude Code 静默流收到心跳（R-C，桥生效）；瞬态失败绑定保持（R-D）；权重生效（R-E）
3. config.example.json / README 同步（R-006）
4. 双席审计（GLM-5.3 / Step-5）返工闭环 + **第三席复审（OpenCode/step-5-preview-free，ACCEPT WITH NITS）F-1~F-14 全部落实（v3）**；教训合入 rubrics
