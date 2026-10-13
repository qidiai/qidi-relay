@echo off
chcp 65001 >nul
cd /d %~dp0
title Codely Relay - 局域网代理

REM 首次运行自动安装依赖（已装则秒过）
py -3 -c "import flask, httpx" >nul 2>&1
if errorlevel 1 (
  echo 首次运行，正在安装依赖 flask httpx ...
  py -3 -m pip install -q flask httpx -i https://pypi.tuna.tsinghua.edu.cn/simple
)

echo ============================================
echo   Codely Relay  局域网代理服务
echo   管理界面: http://localhost:8765
echo   (端口在 config.json 的 "port" 中修改)
echo ============================================
py -3 main.py
pause
