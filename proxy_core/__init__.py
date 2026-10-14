"""proxy_core — QiDi Relay 局域网代理的核心业务包。

模块划分：
    settings  常量、路径、配置加载
    accounts  账号池管理（AccountManager）
    keystore  cli_api_key 缓存 + LiteLLM 请求签名
    stats     Token 用量统计（含小时级历史）+ 实时活动日志
    auth      管理密码哈希与鉴权装饰器
    state     全局单例（账号池 / 缓存 / 统计 / 活动 / 启动时间）
    providers 外部模型源（含每 provider 代理出口与共享 Client 缓存）
    cooldown_store  冷却状态持久化（吸收-1 §R-A）
    classify_failure  失败面 × hard/transient 分类（吸收-1/2 §R-D）
"""
