#!/usr/bin/env bash
# QiDi Relay 一键部署脚本（Linux 服务器）
# 用法: 在部署包解压目录内执行  bash deploy.sh
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "== QiDi Relay 部署到 $APP_DIR =="

# 1. Python 检查（需 3.10+，用了 str | None 语法）
if ! command -v python3 >/dev/null; then
  echo "❌ 未找到 python3，请先安装: apt install -y python3 python3-pip 或 yum install -y python3"
  exit 1
fi
PYVER=$(python3 -c 'import sys; print(sys.version_info[0]*10+sys.version_info[1])')
if [ "$PYVER" -lt 310 ]; then
  echo "❌ Python 版本过低 ($(python3 -V))，需要 3.10+"
  exit 1
fi

# 2. 依赖（用国内镜像加速）
python3 -m pip install -q flask httpx -i https://pypi.tuna.tsinghua.edu.cn/simple 2>/dev/null \
  || python3 -m pip install -q flask httpx

# 3. 权限收紧：数据文件只允许属主读写（含明文 token）
chmod 600 accounts.json config.json 2>/dev/null || true
chmod 700 logs 2>/dev/null || mkdir -p logs && chmod 700 logs

# 4. systemd 服务
SVC=/etc/systemd/system/qidi-relay.service
sudo tee "$SVC" >/dev/null <<EOF
[Unit]
Description=QiDi Relay (LAN AI proxy)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$(which python3) $APP_DIR/main.py
Restart=always
RestartSec=5
# 安全加固
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$APP_DIR

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl disable --now codely-relay 2>/dev/null || true  # 清理旧服务名（若有）
sudo systemctl enable --now qidi-relay
sleep 2
sudo systemctl --no-pager -l status qidi-relay | head -n 8

# 5. 防火墙提示
echo
echo "== 部署完成 =="
echo "服务状态: systemctl status qidi-relay"
echo "日志: journalctl -u qidi-relay -f  或  $APP_DIR/proxy.log"
echo
echo "⚠ 别忘了防火墙只放行需要的端口："
echo "   sudo ufw allow 8765/tcp        # 或云厂商安全组放行 8765"
echo "   建议安全组只放行你的常用 IP 段，而不是 0.0.0.0/0"
echo
echo "验证: curl http://127.0.0.1:8765/health"
