@echo off
chcp 65001 >nul
cd /d %~dp0
title QiDi Relay v5 - 8765 生产
echo ============================================
echo   QiDi Relay v5  局域网代理 (生产)
echo   管理界面: http://localhost:8765
echo   v2 已退役，恢复v2: 双击 v2目录 启动代理-v2.bat
echo ============================================
py -3 main.py
pause
