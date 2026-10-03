"""全局单例：账号池 / Key 缓存 / 统计 / 活动日志 / 启动时间。"""

from datetime import datetime

from .accounts import AccountManager
from .keystore import ApiKeyCache
from .stats import ActivityLog, TokenStats

am = AccountManager()
api_cache = ApiKeyCache()
token_stats = TokenStats()
activity = ActivityLog()

SERVER_STARTED_AT = datetime.now()
