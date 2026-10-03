@echo off
chcp 65001 >nul
cd /d %~dp0
title QiDi Relay v4 - 局域网代理
echo ============================================
echo   QiDi Relay v4  局域网代理服务
echo   管理界面: http://localhost:8765
echo   (端口在 config.json 的 "port" 中修改)
echo ============================================
py -3 main.py
pause
