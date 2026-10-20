"""qoder_scheduler.py —— 后台定时调度器 (Scheduler)

负责常驻后台自动执行：
1. 每日签到 (Daily Checkin)：每日 09:00 / 21:00 为所有账号自动签到领积分。
2. 福利包巡检：签到巡检时顺带刷新额度与套餐快照。
3. Token 保活 (Keepalive)：每日 22:00 集中刷新；另对剩余寿命不足 4 小时的
   账号在任意巡检中提前刷新（drt- / jrt- 按 token 前缀路由，PAT 兜底）。
4. 状态持久化与看板展示：暴露状态、执行记录、支持手动立即触发与开关切换。

状态落盘（本轮修复）：enabled / last_run_time / next_run_time / logs 以及
「启动补签当日已做过」标记写入 <账号目录>/scheduler/state.json。
  · 放在账号目录的**子目录**里：AccountPool.load() 只扫顶层 *.json
    （qoder_accounts.py:1441-1460），子目录不会被误当成账号；
  · accounts/* 已被 .gitignore 忽略，无需新增忽略规则；
  · 目的：进程重启不再产生重复领取——启动巡检每天最多补签一次
    （mark-before-act：先打标记落盘再动作，崩溃重启也不会重放）。
"""
import json
import os
import sys
import threading
import time

import qoder_tasks
from qoder_tasks import set_logger, run_batch_checkin, run_keepalive

# 状态文件相对账号目录的子路径（避免被 AccountPool 读取为账号 JSON）
STATE_SUBDIR = "scheduler"
STATE_FILE_NAME = "state.json"

# 巡回来由：启动自动 / 整点排程 / 手动触发
CYCLE_STARTUP = "startup"
CYCLE_HOUR = "hour"
CYCLE_MANUAL = "manual"


class Scheduler(object):
    def __init__(self, pool, state_dir=None):
        self.pool = pool
        # 对齐社区默认排程 (本地时区 24 小时制)
        self.checkin_hours = [9, 21]     # 每日 09:00、21:00 签到
        self.keepalive_hours = [22]      # 每日 22:00 集中 Token 保活
        self.all_hours = sorted(set(self.checkin_hours + self.keepalive_hours))
        self._stop_event = threading.Event()
        self._thread = None
        self._run_lock = threading.Lock()
        # 状态目录：优先显式传入（测试）→ 账号池目录 → 环境变量 → 脚本同级 accounts/
        self.state_dir = (state_dir or getattr(pool, "dir", None)
                          or os.environ.get("ACCOUNTS_DIR")
                          or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "accounts"))
        self._state_lock = threading.Lock()
        st = self._load_state()
        self.enabled = bool(st.get("enabled", True))   # 看板开关重启后仍生效
        self.last_run_time = st.get("last_run_time") or None
        self.next_run_time = None
        self.logs = [str(x) for x in (st.get("logs") or [])][-60:]
        # 「启动补签」当日标记：进程重启 N 次，同一天最多补签 1 次
        self.startup_claim_date = str(st.get("startup_claim_date") or "")
        self.last_cycle_date = str(st.get("last_cycle_date") or "")
        self._calc_next_fire()
        # 把任务层失败（死端点、上游结构变化）也打进看板日志。
        set_logger(self.log)
        if st:
            self.log("调度器状态已从 %s 恢复（enabled=%s，上次运行 %s）"
                     % (self._state_path(), self.enabled,
                        self.last_run_time or "-"))

    # -- 状态持久化 ---------------------------------------------------------
    def _state_path(self):
        return os.path.join(self.state_dir, STATE_SUBDIR, STATE_FILE_NAME)

    def _load_state(self):
        """读状态文件；不存在/损坏都返回 {}（按全新进程处理）。"""
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self):
        """原子落盘（tmp + os.replace，同账号文件的写法）。失败只写 stderr。

        注意：本函数**不得**调用 self.log()，否则与 log→_save_state 形成递归。
        """
        with self._state_lock:
            try:
                path = self._state_path()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                data = {
                    "enabled": bool(self.enabled),
                    "last_run_time": self.last_run_time,
                    "next_run_time": self.next_run_time,
                    "startup_claim_date": self.startup_claim_date,
                    "last_cycle_date": self.last_cycle_date,
                    "logs": self.logs[-60:],
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception as exc:
                try:
                    sys.stderr.write("[scheduler] 状态落盘失败: %s\n" % exc)
                    sys.stderr.flush()
                except Exception:
                    pass

    def log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = "[%s] %s" % (ts, msg)
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        # 日志即状态：顺带落盘，看板开关/上次运行时间在重启后仍然可见
        self._save_state()
        try:
            import qoder_proxy
            qoder_proxy.add_log_entry("[调度器] %s" % msg, tag="scheduler")
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动")

    def stop(self):
        self._stop_event.set()
        self.log("后台定时调度器已暂停")

    def _run_loop(self):
        # 启动后先休眠 10 秒等待主服务就绪，然后执行初次检查
        time.sleep(10)
        try:
            self._execute_cycle("启动初次初始化巡检", CYCLE_STARTUP)
        except Exception as exc:
            self.log("初次巡检异常: %s" % exc)

        while not self._stop_event.is_set():
            self._calc_next_fire()
            now = time.localtime()
            cur_hour = now.tm_hour
            cur_min = now.tm_min
            if self.enabled:
                if cur_min == 0 and cur_hour in self.all_hours:
                    reason = "整点排程命中 (%d:00)" % cur_hour
                    try:
                        self._execute_cycle(reason, CYCLE_HOUR)
                    except Exception as exc:
                        self.log("排程执行异常: %s" % exc)
                    time.sleep(65)   # 避开当前这一分钟重复触发
            self._stop_event.wait(30)

    def _calc_next_fire(self):
        now = time.localtime()
        cur_h = now.tm_hour
        next_h = None
        for h in self.all_hours:
            if h > cur_h or (h == cur_h and now.tm_min == 0 and now.tm_sec < 10):
                next_h = h
                break
        if next_h is not None:
            t_struct = time.struct_time(
                (now.tm_year, now.tm_mon, now.tm_mday, next_h, 0, 0, 0, 0, -1))
        else:
            t_tomorrow = time.time() + 86400
            now_tom = time.localtime(t_tomorrow)
            first_h = self.all_hours[0]
            t_struct = time.struct_time(
                (now_tom.tm_year, now_tom.tm_mon, now_tom.tm_mday, first_h, 0, 0, 0, 0, -1))
        self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S", t_struct)

    def trigger_now(self):
        """手动立即触发一次调度检查（不受"启动补签当日一次"闸门限制）。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle,
                         args=("手动立即触发", CYCLE_MANUAL),
                         daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检", cycle_kind=CYCLE_HOUR):
        if not self._run_lock.acquire(blocking=False):
            self.log("跳过本次巡检 (%s)：上一轮仍在执行" % trigger_reason)
            return
        try:
            self._run_cycle(trigger_reason, cycle_kind)
        finally:
            self._run_lock.release()

    def _allow_complement_checkin(self, cycle_kind):
        """启动巡检的补签闸门：**同一天最多补签一次**（其余巡回来由不受限）。

        为什么需要：启动巡检会对 can_checkin() 为真的账号发真实领取请求，
        进程反复重启就会反复领取。这里做 mark-before-act——先把当日标记落盘，
        再去做领取；即使随后进程崩溃/被杀，重启后也不再重放。
        can_checkin() 依赖的 per-account lastCheckin（落盘在账号 JSON）是第二道闸。
        """
        if cycle_kind != CYCLE_STARTUP:
            return True
        today = time.strftime("%Y-%m-%d")
        if self.startup_claim_date == today:
            return False
        self.startup_claim_date = today
        self._save_state()
        return True

    def _run_cycle(self, trigger_reason="周期巡检", cycle_kind=CYCLE_HOUR):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_cycle_date = time.strftime("%Y-%m-%d")
        self.log("开始执行任务 (%s)..." % trigger_reason)
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            self._save_state()
            return

        cur_hour = time.localtime().tm_hour
        keepalive_due = cur_hour in self.keepalive_hours
        checkin_due = cur_hour in self.checkin_hours
        allow_complement = self._allow_complement_checkin(cycle_kind)

        # 1. Token 保活：整点 22:00 全量刷新；其余巡检只刷新临近过期的
        force = keepalive_due
        ka = run_keepalive(self.pool, force=force)
        self.log("Token 保活：刷新 %d 个，失败 %d 个%s"
                 % (ka["refreshed"], ka["failed"],
                    "（22:00 集中保活）" if force else "（临近过期）"))
        for line in ka["logs"]:
            if line.startswith("!"):
                self.log(line)

        # #25 a：本轮失效凭证的账号**整体排除**在后续步骤之外。
        # 理由：跳过只是"不再打上游"，但一轮里若已用失效 token 拿到过退化响应，
        # 那次响应会被写进账本（把健康余额覆盖成 0）；所以这里在**源头**把这些
        # 账号从签到与额度快照里去掉 —— 它们的余额等下一次成功刷新再恢复。
        # 两类来源：① 刚才保活明确失败的 uid；② token 已过期（含没被保活覆盖的）。
        stale_uids = set(ka.get("failed_uids") or [])
        now_ts = time.time()
        for a in (self.pool.accounts if self.pool else []):
            exp = getattr(a, "expires_at", 0) or 0
            if a.access_token and exp and exp <= now_ts:
                stale_uids.add(a.uid)
        if stale_uids:
            self.log("跳过 %d 个失效凭证账号（本轮不签到、不刷额度）：%s"
                     % (len(stale_uids),
                        "、".join(sorted(str(u)[:8] for u in stale_uids))))

        # 2. 每日签到：整点签到窗口内执行；其余巡检只补签未签账号
        #    （能力运行时探测：接口不存在的区域由 run_checkin 给出原因并跳过）
        targets = [a for a in self.pool.accounts
                   if a.enabled and a.access_token and a.uid not in stale_uids]
        if checkin_due:
            pending = targets          # 09:00 / 21:00 窗口不受启动闸门影响
        elif allow_complement:
            pending = [a for a in targets if a.can_checkin()]
        else:
            pending = []               # 当日已补签：不发起任何领取
        checkin_count = 0
        earned = 0
        if pending:
            self.log("检测到 %d 个账号需要签到，执行自动签到..." % len(pending))
            res = run_batch_checkin(pending, gap=1.0, inter_gap=1.0)
            checkin_count = res["accounts_count"]
            earned = res.get("credit_added") or 0
            for line in res["logs"]:
                if line.startswith("✓") or line.startswith("!"):
                    self.log(line)
        elif checkin_due:
            self.log("所有账号今日已签到")

        # 3. 刷新额度快照（看板积分卡片依赖）
        for acc in targets:
            try:
                acc.fetch_credits()
            except Exception:
                pass
            time.sleep(0.5)

        self.log("巡检完成：Token 保活 %d 个，签到 %d 个，本次新增积分 +%d"
                 % (ka["refreshed"], checkin_count, earned))
        self._save_state()

    def status(self):
        return {
            "enabled": self.enabled,
            "mode": "整点排程 (09:00/21:00 签到 · 22:00 Token 保活)",
            "mode_cn": "整点排程 (09:00/21:00 每日签到 · 22:00 Token 保活)",
            "mode_intl": "整点排程 (09:00/21:00 每日签到 · 22:00 Token 保活)",
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "startup_claim_date": self.startup_claim_date or "",
            "last_cycle_date": self.last_cycle_date or "",
            "state_file": self._state_path(),
            "logs": self.logs[-20:],
        }
