"""QiDi Relay — 账号池局域网代理服务 v4

- 多账号管理 / 自动轮换 / 配额用尽自动禁用
- OpenAI 兼容 API（chat/completions + responses）
- Token 用量统计（含小时级趋势）与实时请求动态
- 模块化结构：proxy_core（核心）+ routes（路由）+ web（前端）

启动：py -3 main.py（端口在 config.json 的 "port" 配置，默认 8765）
"""

import logging
import logging.handlers
import queue

from flask import Flask, render_template

from proxy_core import settings
from proxy_core.settings import LOG_FILE, VERSION, get_port, load_config
from proxy_core.state import am  # noqa: F401  (导入即初始化单例)
from routes.admin import bp as admin_bp
from routes.proxy import bp as proxy_bp

# 日志异步化（0831 卡死根因修复）：
# 旧版双写 文件+控制台（StreamHandler）。WMI 拉起的进程没有可见控制台，
# stdout 句柄无效时 stream.write 会永久挂起 → 持有 Handler 锁 → 17 个请求
# 线程全部排队等日志锁 → 整个服务假死（端口在监听但 /health 无响应）。
# 修复：只保留文件日志，且用 QueueHandler 把写盘挪到后台线程——
# 请求线程只往内存队列丢日志，磁盘再卡也只丢日志不卡请求。
_log_q = queue.SimpleQueue()
_queue_handler = logging.handlers.QueueHandler(_log_q)
# 防止 basicConfig 的默认格式 %(levelname)s:%(name)s:%(message)s 注入消息
# （QueueHandler.prepare 会用它预格式化 record.msg，导致日志出现双前缀）
_queue_handler.setFormatter(logging.Formatter("%(message)s"))
_file_handler = logging.handlers.RotatingFileHandler(
    str(LOG_FILE), encoding="utf-8", maxBytes=50 * 1024 * 1024, backupCount=3
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
_listener = logging.handlers.QueueListener(
    _log_q, _file_handler, respect_handler_level=True
)
_listener.start()

logging.basicConfig(level=logging.INFO, handlers=[_queue_handler])
log = logging.getLogger("qidi-relay")

app = Flask(
    __name__,
    static_folder="web/static",
    static_url_path="/static",
    template_folder="web",
)
app.register_blueprint(proxy_bp)
app.register_blueprint(admin_bp)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/knowledge")
def knowledge():
    return render_template("knowledge.html")


@app.route("/free-tokens")
def free_tokens():
    """公开栏目：免费 Token 资源分享（PUBLIC，无需登录）。

    对外分享用页面：生产环境实测过的免费/低价 token 资源清单。
    刻意注册在 app 上而非 admin 蓝图——蓝图 url_prefix="/api"，
    页面路径应为 /free-tokens（nginx 8443 → relay 全量转发），
    与 / 、/knowledge 同级、同样不挂 @check_admin_auth。
    """
    return render_template("free-tokens.html")


if __name__ == "__main__":
    port = get_port()
    # F 小修：config.json 重复键（大小写折叠后重复，解析时
    # 后者覆盖前者）启动时逐条 error 点名（收集逻辑见
    # proxy_core.settings.load_config）
    load_config()
    for _warn in settings.CONFIG_DUPLICATE_KEY_WARNINGS:
        log.error("config.json 重复键: %s", _warn)
    log.info("=" * 50)
    log.info("QiDi Relay v%s 启动中...", VERSION)
    log.info("端口: %d", port)
    log.info("账号数: %d", len(am.accounts))
    for a in am.accounts:
        log.info("  - 账号 %s (@%s) 已加载", a["user_id"], a.get("username", ""))
    if not am.accounts:
        log.info("⚠ 没有可用账号！请在管理界面添加账号")
    log.info("=" * 50)
    # 种子卫兵（0905 吸收自 ProxyDeck）：后台核对官方 CLI 签名特征，轮换早预警
    try:
        from proxy_core.seedguard import start_background
        start_background()
    except Exception as e:
        log.warning("种子卫兵启动失败（不影响代理服务）: %s", e)
    # 语料采集（阶段 E / 方案 §7）：默认关闭（config
    # corpus.enabled=true 显式开启），后台 worker 线程
    # 负责脱敏+落盘（§7.3 红线：请求线程只入队）
    try:
        from proxy_core.corpus import start_worker
        start_worker()
    except Exception as e:
        log.warning("语料采集启动失败（不影响代理服务）: %s", e)
    app.run(host="0.0.0.0", port=port, debug=False)
