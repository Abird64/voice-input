#!/bin/bash
# 语音输入 — 自动安装 & 管理脚本
#
# 首次使用:  ./voice-input.sh install   (一键安装: systemd自启 + 快捷键注册)
# 日常:      守护 + STT 自动启动，按 Ctrl+/ 即可
#
# 管理: ./voice-input.sh {start|stop|status|toggle|install|uninstall}

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/voice-input"
STT_PID_FILE="$RUNTIME_DIR/stt-server.pid"
DAEMON_PID_FILE="$RUNTIME_DIR/daemon.pid"
DAEMON_SOCK="$RUNTIME_DIR/daemon.sock"
TOKEN_FILE="$RUNTIME_DIR/token"
CUSTOM_KEYBINDING_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-input/"

export PATH="$HOME/.local/share/pi-node/current/bin:/usr/bin:/usr/local/bin:$PATH"
export HOME="$HOME"
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
NODE="$(which node)"

ensure_runtime_dir() { mkdir -p "$RUNTIME_DIR"; }

send_signal() {
  python3 -c "
import socket
s=socket.socket(socket.AF_UNIX)
try:
    s.connect('$DAEMON_SOCK')
    s.send(b'$1')
    s.close()
except: pass
" 2>/dev/null
}

daemon_running() {
  [ -f "$DAEMON_PID_FILE" ] && kill -0 "$(cat "$DAEMON_PID_FILE")" 2>/dev/null
}

# ── 快捷键注册 ─────────────────────────────────────────────
register_keybinding() {
  local binding="${1:-<Control>slash}"
  local command="${2:-$SCRIPT_DIR/voice-input.sh toggle}"

  # 检查是否已存在
  local existing
  existing=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null || echo "[]")

  if echo "$existing" | grep -q "voice-input"; then
    echo "  ⌨️  快捷键已注册 (Ctrl+/)"
    return 0
  fi

  # 添加 voice-input 路径
  local new_list
  new_list=$(echo "$existing" | sed 's/]$//')
  if [ "$new_list" = "[" ]; then
    new_list="['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-input/']"
  else
    new_list="${new_list}, '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-input/']"
  fi

  gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$new_list"
  gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"$CUSTOM_KEYBINDING_PATH" name '语音输入'
  gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"$CUSTOM_KEYBINDING_PATH" binding "$binding"
  gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:"$CUSTOM_KEYBINDING_PATH" command "$command"
  echo "  ⌨️  快捷键已注册: Ctrl+/ → 语音输入"
}

unregister_keybinding() {
  local existing
  existing=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null || echo "[]")
  if echo "$existing" | grep -q "voice-input"; then
    local new_list
    new_list=$(echo "$existing" | sed "s|'/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/voice-input/',\?||g" | sed 's/,,/,/g')
    gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$new_list" 2>/dev/null || true
    echo "  ⌨️  快捷键已移除"
  fi
}

# ── 命令 ──────────────────────────────────────────────────
case "${1:-start}" in
  start)
    ensure_runtime_dir

    # — STT 推理服务器 —
    if [ -f "$STT_PID_FILE" ] && kill -0 "$(cat "$STT_PID_FILE")" 2>/dev/null; then
      :
    else
      TOKEN=$(openssl rand -hex 16 2>/dev/null || head -c16 /dev/urandom | xxd -p)
      echo "$TOKEN" > "$TOKEN_FILE"
      STT_TOKEN="$TOKEN" nohup "$NODE" "$SCRIPT_DIR/stt-server.js" \
        > "$RUNTIME_DIR/stt-server.log" 2>&1 &
      echo "$!" > "$STT_PID_FILE"
      sleep 2
    fi

    # — 语言守护 + 悬浮窗 —
    if daemon_running; then
      :
    else
      nohup python3 "$SCRIPT_DIR/voice-daemon.py" \
        > "$RUNTIME_DIR/daemon.log" 2>&1 &
      echo "$!" > "$DAEMON_PID_FILE"
      sleep 2
    fi

    register_keybinding
    echo "✅ 语音输入就绪"
    ;;

  stop)
    if daemon_running; then
      send_signal "quit" || true
      kill "$(cat "$DAEMON_PID_FILE")" 2>/dev/null || true
      rm -f "$DAEMON_PID_FILE"
    fi
    [ -f "$STT_PID_FILE" ] && kill "$(cat "$STT_PID_FILE")" 2>/dev/null && rm -f "$STT_PID_FILE"
    echo "🛑 已停止"
    ;;

  toggle)
    if ! daemon_running; then
      "$0" start
      sleep 1
    fi
    send_signal "toggle"
    ;;

  status)
    echo "=== 语音输入状态 ==="
    if [ -f "$STT_PID_FILE" ] && kill -0 "$(cat "$STT_PID_FILE")" 2>/dev/null; then
      echo "🎤 STT 模型: ✅ 运行中"
    else
      echo "🎤 STT 模型: ❌ 未运行"
    fi
    if daemon_running; then
      echo "🪟 守护窗口: ✅ 运行中"
    else
      echo "🪟 守护窗口: ❌ 未运行"
    fi
    echo "⌨️  快捷键 : Ctrl+/ (toggle)"
    ;;

  install)
    echo "📦 一键安装语音输入..."

    # 1. systemd user service — STT 服务器
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/voice-input-stt.service" << SERVICE
[Unit]
Description=Voice Input STT (SenseVoice)
After=default.target
[Service]
Type=simple
ExecStart=${NODE} ${SCRIPT_DIR}/stt-server.js
Environment="STT_TOKEN_FILE=${TOKEN_FILE}"
Environment="PATH=${HOME}/.local/share/pi-node/current/bin:/usr/bin:/usr/local/bin"
Restart=on-failure
RestartSec=5
[Install]
WantedBy=default.target
SERVICE

    # 2. systemd user service — 语音守护
    cat > "$HOME/.config/systemd/user/voice-input-daemon.service" << SERVICE
[Unit]
Description=Voice Input Daemon (GTK4 Window)
After=voice-input-stt.service
Requires=voice-input-stt.service
[Service]
Type=simple
ExecStart=python3 ${SCRIPT_DIR}/voice-daemon.py
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0
Environment=XDG_RUNTIME_DIR=/run/user/%U
Restart=on-failure
RestartSec=3
[Install]
WantedBy=default.target
SERVICE

    systemctl --user daemon-reload
    systemctl --user enable voice-input-stt.service voice-input-daemon.service
    systemctl --user start voice-input-stt.service voice-input-daemon.service

    # 3. 注册全局快捷键
    register_keybinding

    echo ""
    echo "✅ 安装完成！"
    echo ""
    echo "🎤 按 Ctrl+/ 开始语音输入"
    echo "📋 识别结果窗口弹出 → Ctrl+C 复制"
    echo ""
    echo "管理:"
    echo "  systemctl --user status voice-input-daemon"
    echo "  ~/voice-input/voice-input.sh status"
    ;;

  uninstall)
    echo "🗑️  卸载语音输入..."
    systemctl --user stop voice-input-stt.service voice-input-daemon.service 2>/dev/null || true
    systemctl --user disable voice-input-stt.service voice-input-daemon.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/voice-input-stt.service"
    rm -f "$HOME/.config/systemd/user/voice-input-daemon.service"
    systemctl --user daemon-reload
    unregister_keybinding
    "$0" stop 2>/dev/null || true
    echo "✅ 已卸载"
    ;;

  *)
    echo "语音输入 — 全局语音转文字 (GNOME Wayland)"
    echo ""
    echo "  ./voice-input.sh install    — 一键安装 (systemd 自启 + Ctrl+/)"
    echo "  ./voice-input.sh uninstall  — 卸载"
    echo "  ./voice-input.sh start      — 手动启动守护"
    echo "  ./voice-input.sh stop       — 停止所有"
    echo "  ./voice-input.sh status     — 查看状态"
    echo "  ./voice-input.sh toggle     — 切换录音 (快捷键绑定用)"
    ;;
esac