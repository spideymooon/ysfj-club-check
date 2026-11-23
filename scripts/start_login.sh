#!/bin/sh
# 按需启动的「人工重新登录」服务：
#   Xvfb(虚拟显示) -> x11vnc -> noVNC(websockify) -> 有头 Chrome
# 浏览器访问 http://<NAS_IP>:6080/vnc.html 即可看到登录页面，用手机号验证码登录。
# 登录态写入与主服务共享的 /app/data/browser_profile 卷。
set -eu

export DISPLAY=:99
: "${LOGIN_WAIT_SECONDS:=900}"

echo "[login] starting Xvfb on :99 ..."
Xvfb :99 -screen 0 1440x900x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
sleep 1

if [ -n "${VNC_PASSWORD:-}" ]; then
    echo "[login] x11vnc with password"
    x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null 2>&1
    x11vnc -display :99 -forever -shared -rfbport 5900 -rfbauth /tmp/vncpass -quiet \
        >/tmp/x11vnc.log 2>&1 &
else
    echo "[login] x11vnc without password (仅建议内网使用，勿暴露到公网)"
    x11vnc -display :99 -forever -shared -rfbport 5900 -nopw -quiet \
        >/tmp/x11vnc.log 2>&1 &
fi

echo "[login] noVNC on :6080"
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
sleep 1

echo "============================================================"
echo "  请在浏览器打开: http://<NAS_IP>:6080/vnc.html"
echo "  完成手机号验证码登录后，页面跳回 club.ysjf.com 即自动保存登录态。"
echo "============================================================"

export HEADLESS=false
# 忽略 compose 传入的多余参数，固定执行 login
exec python -m app.main login
