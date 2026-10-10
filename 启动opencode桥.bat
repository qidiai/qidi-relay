@echo off
chcp 65001 >nul
cd /d %~dp0
title opencode serve 桥 - 4096
echo ============================================
echo   OpenCode serve 桥（OpenCode Go 订阅 free 模型通道）
echo   监听: http://127.0.0.1:4096
echo   健康检查: 浏览器打开 http://127.0.0.1:4096/doc
echo   最小化窗口勿关；桥掉线该通道判负换号，其余通道不受影响。
echo ============================================
start "opencode-bridge" /min opencode serve --port 4096
echo.
echo 已最小化启动桥窗口（标题 opencode-bridge）。
echo 关闭本提示框不影响桥运行；要停桥请关闭那个最小化窗口。
pause
