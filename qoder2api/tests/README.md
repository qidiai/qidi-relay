# tests/ —— 套件化测试（P0-2 骨架）

> **能让机器自证的，别写成对人的要求** —— 产物自证（跑出来就带）优于检查项（需要有人去比对）：
> 前者连「忘了核对」这个失败模式都消掉了。本文件下面几节（观测点原则 / 迁移策略 / 桩的保真度 /
> 基线数字）都是这条的不同侧面。

## 怎么跑

    python tests/run_all.py              # 全部套件（并发，一行一套件）
    python tests/run_all.py guards       # 只跑名字含 guards 的
    python _test_qoder.py                # 老入口仍然有效（保留兼容）

编排照抄 wb 的 `tests/run_all.py`：`ThreadPoolExecutor` 并发、每套件写独立日志文件、
失败打印 tail 25 行、子进程强制 `PYTHONIOENCODING=utf-8`、无匹配套件返回 2。

## 现有套件

| 套件 | 覆盖 | 状态 |
|---|---|---|
| `_test_legacy.py` | 把仓根 `_test_qoder.py` 整体接进新编排（**689 条既有断言一条不丢**） | 迁移过渡用 |
| `_test_guards.py` | P0-1 护栏核心不变量（含等号边界 / fail-open / free 豁免 / 全关短路） | 新 |
| `_test_auth_matrix.py` | 面板鉴权矩阵（401 / 200-面板会话 / 403 语义与 401 分离） | 新 |

## 迁移计划（两步交付的第一步已落地）

1. **已完成**：编排骨架 + 兼容入口 + 两个新套件；
2. **待迁**：把 `_test_qoder.py` 的 [1]-[44] 段按主题拆成 `_test_*.py`，每拆一段就从 legacy 里删掉一段，
   直到 `_test_legacy.py` 只剩空壳；
3. **待补的三类空白**：代理不变量（参数穿透式断言）、keep-alive 早拒（裸 socket 复现）、
   前端 DOM 断言（照 wb 的 `_dom_stub.js`）。

## 观测点原则（来自 C3 那五轮的教训）

**判据要拦在对的那一层**：说「没有发上游」就要拦在**网络层**（`http_json`），
而不是「`open_upstream` 是否被调用」—— 后者在正常路径上本来就会被调用（它内部才选号）。

## 本轮落盘实况（task-71 第一步）

**迁移进度：已迁 0 段 / 待迁 44 段。** 既有 689 条断言全部由 `_test_legacy.py` 承载（转发 `_test_qoder.py`），
不丢一条；新增 2 个独立套件（guards 10 条 + auth-matrix 5 条）。

### run_all.py 输出样例

    $ python tests/run_all.py
      _test_auth_matrix.py               PASS
      _test_guards.py                    PASS
      _test_legacy.py                    PASS

    SUMMARY: 3 suites, 3 passed, 0 failed  (logs: <tmp>/qd-suites-xxxx)
    $ echo $?
    0

失败时会额外打印该套件的 tail 25 行；无套件匹配时返回 2（避免「0 passed, 0 failed → exit 0」这种 CI 最怕的假绿）。

### 故意改坏 → 必红（自证记录）

变异 `qoder_accounts.py` 里 `reserve_blocked` 的判定式 `int(float(remain)) <= int(reserve)` → 改成 `<`：

    $ python tests/_test_guards.py
      [FAIL] reserve 含等号：remain==reserve -> 拦  None
    SUMMARY: TOTAL 10 checks, 9 passed, 1 failed
    RESULT: RED (exit 1)

恢复后同一套件 `GREEN (exit 0)`；且 `git diff --stat -- qoder_accounts.py` 为空 —— 变异只在备份副本上做、原文件未留痕。

### 三条提醒的落点

1. **判据拦在对的层**：见 README 上节「观测点原则」，guards 套件直接测判定函数本身；
2. **兼容入口保留**：`python _test_qoder.py` 原样可用，`tests/run_all.py` 通过 `_test_legacy.py` 转发它，两边同源；
3. **两步交付**：这一步只做「能跑、能汇总、能红」的骨架 + 2 个新套件，不追求一次拆完 4873 行。

## 第二步：三类空白套件已补（task-71 · 第一批）

| 套件 | 类型 | 覆盖 |
|---|---|---|
| `_test_proxy_invariant.py` | 不变量 | 带凭证的请求必须走统一出站函数 `http_json`（桩它 → 断言 `fetch_credits` 经过它）；**基线登记**两个文件里直连 `urlopen` 的调用点数量（新增/删除都要重新评估是否绕过收口） |
| `_test_keepalive.py` | 裸 socket | ① 超长请求行（100KB URI）必须**快速拒绝**（实测 414，服务端随后直接关连接，客户端 recv 可能抛 `ConnectionAbortedError` —— 两者都算「没挂起」）；② **同一条连接**连发两个 `/health` 都要 200（读响应必须按 `Content-Length` 读满 body，否则第二次会读到上一次的 body 残片） |
| `_test_dashboard_render.js` | 前端（层 1+2） | 读 `dashboard.html` → 抽所有 `<script>` → **内联约 15 行元素桩**（照 wb `_test_matrix_filters.js` 的写法；工作包里提到的 `_dom_stub.js` 在 wb 里并不存在）→ 断言渲染纯函数四态与 toast 配色，另加一条「日志区是追加式写法」的源码级断言 |

> `tests/` 里现在有 **1 个 .js 套件**（此前 0 个）；`run_all.py` 会自动发现并跳过 node 缺失的环境。

### 能红自证（三条）

1. **guards**：`reserve_blocked` 的 `<=` 改成 `<` → `[FAIL] reserve 含等号` → RED exit 1；恢复后 GREEN；`git diff -- qoder_accounts.py` 无我方痕迹。
2. **proxy_invariant**：往 `qoder_accounts.py` 追加一处直连 `urlopen`（基线断言 == 3）→ RED exit 1；恢复后 GREEN。
3. **dashboard_render**：把 `dashboard.html` 里 `\|\| earned > 0` 改成 `>= 0` →
   `[FAIL] checkinOutcome：earned=0 且无 claimed -> idle（不报签到成功）` + `[FAIL] checkinToastKind：全 idle -> warn` → RED exit 1；恢复后 GREEN exit 0。

   这三条都只动**备份副本**（`%TEMP%` 下先备份、跑完立即还原），原文件不留痕。

## 第三步：上游异常形状 + Anthropic 档位接线（本轮）

| 套件 | 类型 | 覆盖 |
|---|---|---|
| `_test_stream_guards.py` | 守卫（离线纯函数） | 上游信封 body **不保证是 JSON object**（实测有裸数组/标量）。三处守卫 `clean_chunk` / `iter_inner_sse` / `aggregate_stream` 遇非 dict 帧：① 不抛异常；② 不吞后续帧（异常帧夹在中间时正文仍完整）；③ 既有语义不回退（噪声清洗、usage 首个优先、正文折叠）；④ 集成一条：异常帧走完整 Anthropic 流式管线 |
| `_test_anthropic_wiring_meta.py` | 接线（HTTP 层 + 端到端） | `/v1/messages` 的 `thinking.budget_tokens` 要经 `catalog_meta` 才能映射成 `reasoning_effort`。断言拦在 `open_upstream` **唯一出站口**（不是「`messages_to_chat` 被调用过」）：实际交给上游的 `chat_req` 必须带档位、且档位在该模型官方档位表内；另加端到端「档位真的写进上游 `parameters`」与两条静态钉住 |

### 能红自证（本轮两条）

1. **stream_guards**：删掉 `qoder_proxy.py` 里三处 `isinstance(..., dict)` 守卫（回到改动前形状）
   → 6 条 `[FAIL] ... AttributeError: 'list' object has no attribute 'get'` → RED exit 1；恢复后 GREEN 20/20。
2. **anthropic_wiring_meta**：把 `_handle_anthropic` 的 `catalog_meta_for(...)` 下传改回
   `messages_to_chat(payload)` → `[FAIL] model=qmodel_38max：chat_req.reasoning_effort 非空` 等 6 条 → RED exit 1；恢复后 GREEN 16/16。

   同样只动备份副本；跑完 `git diff --stat` 与本轮改动一致，原文件无变异痕迹。

> 两个套件都把自己的 `QD_PROXY_USAGE_DIR` 指到 `tempfile.mkdtemp()`：被桩中止的请求
> 也会走 `record_error` 落盘，不能写进仓库的 `usage/`（并发编排下是跨进程共享路径）。

## 迁移期纪律（每批迁移后都要过一遍）

### 1. 路径：一律基于 `__file__`，绝不用 cwd

套件在 `tests/` 下跑（`run_all.py` 就是 `cwd=tests/`），而仓根文件在上一层。
**不要**用 `os.path.dirname(os.path.abspath(__file__))` 当仓库根 —— 那在 `_test_qoder.py`
里成立（它就在仓根），搬进 `tests/` 就错了。统一用 `_suite_head.ROOT`：

    _src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()

（第 2 批迁移的 `_test_offline_tools.py` 踩过这个，3 处已改；现已验证 `cwd=tests/` 与
`cwd=仓根` 两种跑法都是 32/32 GREEN。）

### 2. 依赖：段里用到的模块必须能在共享头部找到

段落在 legacy 里可能靠「前面某段顺手 import」才拿到 `hashlib` 之类。独立成套件就会
NameError。`_suite_head.py` 已把常用标准库（`base64/contextlib/hashlib/io/json/os/re/
shutil/struct/sys/tarfile/tempfile/time`）全部模块级 import 并放进 `__all__`。

### 3. 隔离：每个套件自己的临时目录、不共享模块级全局

- 临时文件一律 `tempfile.mkdtemp(prefix="qd-<套件名>-")`，`finally` 里 `rmtree`；
- 需要改模块级全局（`P.USAGE_LOG` / `P.POOL` / `P.API_KEY` …）时，**必须** try/finally 恢复；
- **不要写仓库内的共享路径**（`usage/` / `accounts/` / `umid/`）—— 并发编排下那是跨进程共享的，
  是最典型的「单跑绿、并发红」来源；
- 需要独占某个全局资源的套件，可以单独串行跑：`QD_TESTS_JOBS=1 python tests/run_all.py`，
  或 `python tests/run_all.py <套件名片段>`。

### 4. 环境变量在并发下是安全的（但别依赖）

`run_all.py` 每个套件跑在**独立子进程**里，所以 `os.environ[...]` 的读写不会跨套件串味。
但**同一进程内**的多个断言互相影响仍要防（改完要恢复）。

### 5. 「单跑绿、并发红」的排查顺序

1. 先跑 `QD_TESTS_JOBS=1 python tests/run_all.py`（串行）—— 若串行也红，那是套件自身问题；
2. 串行绿、并发红 → 查上面的第 3 条（共享路径/全局）；
3. 还是找不到 → 跑两遍 `run_all.py` 对比日志（`<tmp>/qd-suites-*/`），看失败是否稳定复现。

## 迁移策略（第三批回滚后修订 · Lead 定稿）

**目标不是「迁完 44 段」，而是「测试结构持续变好、每一步都不带风险」。**

三次回滚各暴露一个新类型的坑，全部已内建到流程里：

1. **迁移与修 bug 不能混在同一次编辑** —— 回滚困难、定位困难；
2. **共享头部必须抽出来**（`_suite_head.py`）—— 各套件自己抄会漏 import；
3. **段可能有全局副作用** —— 段的全局绑定/定义可能被**后续段继承**（如 `[11]` 段的
   `time = _time_for_11`、`_A`、`_orig_sleep2`）。搬走就让后面的段 NameError，而新套件
   自己反而是绿的 → **legacy 只在整体跑时才暴露**。

### 当前策略

- **优先迁叶子段**（无后续引用的）；判定用 `python tests/_tools/dep_scan.py`
  （它会列出每个段定义的全局名 + 哪些后续段在用它们；`--leaf-only` 只看叶子段）；
- **非叶子段成组迁，或直接跳过** —— 跳过是可接受的；
- **legacy 是「遗留套件」而不是技术债**：它 626 条断言在跑、全绿，是一个稳定态；
- **新代码的测试一律走新套件** —— 渐进替换，不背「必须搬完」的包袱。

### 实测（2026-10-09）

`dep_scan.py --leaf-only` 当前**没有输出** —— 跑完 55 个段后，**没有一个段是纯叶子**：
段与段之间共享大量全局（`time`/`base64`/`payload`/`url` 等是显式共享，`_A`/`_orig_sleep2`
这类是隐式继承）。这与第三批的回滚现象完全一致，也直接支持「legacy 作终态」的结论。

---

## 夹具纪律：**所有出站函数默认桩住**（2026-10-13，由一次真实踩坑立规）

**事故**：写 #25 的「文案矩阵」探针时只桩了 `Account.campaigns()`，漏桩 `Account.claim_campaign()`。
夹具里那条 `CLAIMABLE` 活动于是走了真实 `claim_campaign` —— 探针**真的向 qoder.sh 打了一次
claim POST**（用的是假 token，回 401 `TOKEN_EXPIRE`，无状态改变，但性质是"离线夹具出网了"）。

**为什么危险**：这类探针通常带着**真实账号的 token**（或仓库里的 accounts/*.json），漏桩就不是
"打一次 401"，而是真的领取/刷新/写账本 —— 夹具本该零副作用。

**规矩**：

1. 夹具**开头**统一把所有出站函数桩掉，再写用例；宁可多桩一行，不要赌"这条路径不会走到"：
   - `qoder_accounts.http_json`（**唯一真正的网络出口**，拦它最划算）；
   - `Account.campaigns` / `claim_campaign` / `checkin` / `campaign_checkin` /
     `fetch_credits` / `fetch_plan` / `refresh` / `native_machine_identity`；
   - `Account.save` / `_stamp_checkin`（防写盘）；
   - 调度器侧 `run_keepalive` / `run_batch_checkin` / `time.sleep`（防真等）。
2. 环境隔离：`ACCOUNTS_DIR` / `QD_PROXY_USAGE_DIR` 指向 `tempfile.mkdtemp()`，
   `QD_NATIVE_IDENTITY=0`（不拉起官方原生桥）。
3. **自证**：夹具跑完打印一条"出网计数"（例如 `http_json` 的调用次数）—— 期望 0；
   非 0 就是漏桩，直接判红。
4. 变异/能红证据只在**副本**上做（见上文 reserve_blocked 的示范），原文件不留痕。

## 桩的保真度纪律（棉花糖这轮踩出来的两条）

这两条与前面记的「锚点缺失即抛」「按函数而非区间做纯度检查」是同一类：**桩的保真度**
直接决定断言的可信度 —— 桩太粗会假绿，桩不对会假红。

### 1. 元素桩要照 DOM 语义做

`textContent` / `innerHTML` 的 setter 会把值转成字符串。用普通对象字面量当元素桩时，
`el.textContent = 1234` 之后读回来是 `'1234'`，断言 `el.textContent === 1234` 会直接红
（实测：棉花糖第一版就这么红了一次）。正确做法：

    const el = {};
    Object.defineProperty(el, 'textContent', {
      get() { return this._t ?? ''; },
      set(v) { this._t = String(v); },   // 照 DOM 语义：任何值都变字符串
    });

同理，`classList` 需要真的维护一个 Set（否则 `contains()` 恒假、`add()` 无痕），
`children` / `appendChild` 要真的累积 —— 否则「追加 vs 重建」这类行为断言测不出来。

### 2. 被测函数调用的兄弟函数要显式注入空桩

被测函数若会调用同页面的另一个渲染函数（例如 `renderCodePanel`），必须把它作为参数
注入一个空桩。否则一个单元断言会顺带拖进半个页面：任何兄弟函数的改动都会让这条断言红，
而它俩其实没有语义关系。

判据：**一条断言的失败原因应当唯一指向它要守的那件事**。做不到就说明注入面没划清。

## 基线数字（提交前核对用）

**用途**：新增/删除套件时，`python tests/run_all.py` 的 `TOTAL N checks` 是本仓测试覆盖的
**守门数值**。它下降通常意味着「断言少了但测试仍然绿」—— 这是最难发现的一类缺失
（没有红、没有报错，只是覆盖变窄了），所以每次提交前后都值得看一眼。

**基线（v1.3.7 · 2026-10-13）**：

| 口径 | 数值 |
|---|---|
| 套件数 | **28 suites** |
| 全部断言 | **1005 checks** |
| 结果 | 0 failed / exit 0 |
| 前端六套件小计 | **105**（9/19/19/15/12/31） |
| legacy 套件 | 630（其中 4 条 SKIP，语义见 `_test_qoder.py` 的 SUMMARY 说明） |

**提交前建议三步**：

1. `python tests/run_all.py`，确认 `0 failed` 且 `TOTAL` 不低于上表；
2. `git status --porcelain`，确认没有 `_mig*` / `*.bak*` / `*.mutbak` 之类的临时残留；
3. `git add -A`（**不要只用具体文件名 add** —— 新增的套件文件会被静默漏掉，表现为
   「测试仍然全绿，但总数比基线低」，例如 105 变 97）。

## 指纹行（每个套件都自带）

每个套件在开头打印两行（前端 JS 与后端 Python **同格式**）：

    SUITE: _test_xxx.py                     md5=<套件自身全文 md5>
    FILE: qoder_proxy.py md5=<...> bytes=<...> as-of=<ISO-8601 UTC Z>

**为什么要它**：出过一次「同一份代码在不同人手里是不同版本」的误报 —— 报告方说「我跑的是新版」，
复核方跑的是旧版，双方都对，但结论相反。**只报 md5 不够，因为 `md5=x` 在两个时点可以同时为真**，
所以还要 `as-of` 时间戳；只报被测文件也不够，因为「你跑的是哪一份」问的是**套件**，所以还要 `SUITE:`。

**规矩（一个套件可能依赖多个被测文件时）**：

- 每个套件列出它**主要守护**的被测文件，**1~4 个**，不追求列全；
- 判据是「本套件失败时，看指纹行就知道跑的是哪份产物」；
- 实现只在 `tests/_suite_head.py` 的 `declare_suite(suite_file, files)` 一处，各套件 import 调用；
- 量纲分开标名：`bytes=` 是**磁盘字节数**（与 `md5sum` / `Get-FileHash` / `wc -c` 同口径），
  JS 侧另有 `codeunits=` 表示字符串长度 —— 同一个词不混两种量纲；
- `SUITE:` 的 md5 在**启动时**取值（代表被执行的那份字节），不是打印时刻读盘 —— 否则运行中被编辑会误导。

## 改共享模块时的一条纪律

> **改 `_suite_head.py`（或任何被多处依赖的共享模块）时，先列出它的所有消费者，再动手。**

来历：这条教训在本仓出现过**两次**，形状一样 ——

- 第一次：迁移 `_test_qoder.py` 的段落时，段里定义的全局（`time = _time_for_11` / `_A` / `_orig_sleep2`）
  是给**后续段**用的，搬走就让后面的段 NameError，而新套件自己反而是绿的；
- 第二次：给 `_suite_head.py` 加 `declare_suite()` 时忘了加进 `__all__`，4 个用 `from _suite_head import *`
  的套件当场 `NameError`（run_all 显示 24 passed / 4 failed / 4 suites without SUMMARY）。

两次的共性是：**共享的东西一旦被多处依赖，它的每一次扩张都要同时考虑所有依赖方** ——
只想着「新增」而不想「导出面 / 依赖面」，就会造出「本地看着对、整体是崩的」那种失败。

**可执行做法，是前置/后置的一对**：

1. **前置 —— 改之前先列消费者**：`rg -l "_suite_head" tests/`（或对应的导入名），确认这次扩张会影响谁；
2. **后置 —— 改完立刻跑一次全量、看总数对不对**：
   · **这是默认动作，不是补救动作** —— 本仓四次同形的坑（迁移时被搬走的全局 / 共享头部的导出面 /
     单文件语法外引用 `ROOT_DASH` 未定义 …）**没有一次被单套件测试抓到**，全是被**总数变化**抓到的；
   · 判读：总数比基线低、或出现 `(+N suites without SUMMARY)`，就是有套件崩了（不是「跑得慢」）；
   · 起作用的原因很简单：**总数是个不会被局部自测发现的变化** —— 局部测试跑得再绿，也看不见「另一个套件崩了」。

## 证据可得性：证据在哪里，取决于读者会去哪里找

来历：`run_all.py` 的兜底行（套件没自己声明指纹时由 runner 补一条 `SUITE: … (runner-computed)`）
最初只 `print` 到 runner 的 stdout。拿铁复核时**只读了 `logs: <dir>` 里的日志文件**，于是以为「兜底没兜住 JS 套件」，
翻终端输出才发现兜底行在 stdout。

**根因不是实现错，是证据放错了地方**：`logs: <dir>` 这个提示本身就在告诉读者「日志才是留存证据」——
只保存日志的人复查时会重复同一段误判。现在兜底行**同时追加进该套件的 log**，两处都有。

**推广**：写任何诊断/指纹/兜底输出时，别只问「我打出来了吗」，要问「**复查的人会去哪里找**」。
证据出现在读者不会去的地方，等于不存在。

## 可靠性三层：原则 < 动作 < 工具约束 —— 但工具也会说假话

来历：一次批量替换里，锚点没匹配上，**脚本却无条件打印了「已改」** —— 工具说了假话，而我照着它以为改好了。
（拿铁把它归纳成这三层，正好解释为什么「把约定做进工具」比「写成纪律」强，也解释了它为什么还不是终点。）

- **原则**（记不住）< **动作 / checklist**（记得住，但仍依赖人）< **工具约束**（不依赖人）；
- **但工具不是终点**：当工具自己就是说假话的那一环时，上面两层全断。所以
  **工具的成功报告必须来自对结果的验证，而不是对动作的发起**：
  · 批量替换后断言「旧串没了 / 新串在 / 文件长度或哈希变了」（任一不符即报失败）；
  · runner 兜底失败时要打 `runner fallback FAILED: <reason>`，不能静默 `return None` ——
    否则「零触发」与「坏了」在输出上完全一样。
- 一句话：**「我执行了动作」≠「动作产生了预期效果」。**
