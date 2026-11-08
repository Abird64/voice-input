# 🎙️ voice-input

Linux 语音输入工具，GTK4 + Cairo 粒子球动画。

按下 **Ctrl+/** 开始录音，说完自动识别并写入剪贴板。

![orb](https://img.shields.io/badge/orb-particles-blue)

## 功能

- 全局快捷键 Ctrl+/ 切换录音
- 粒子球浮动动画（Fibonacci 球面 + Cairo additive blending）
- 录音中：蓝青色旋转粒子球
- 识别中：紫蓝色脉动粒子球
- 识别结果弹窗，一键复制
- `faster-whisper` 本地 STT，中文优化
- `pw-record` PipeWire 录音，低延迟

## 依赖

```bash
sudo apt install python3-gi python3-cairo gir1.2-gtk-4.0 pipewire
pip install faster-whisper
```

## 安装

```bash
git clone https://github.com/Abird64/voice-input.git
cd voice-input
./voice-input.sh install
```

## 使用

```bash
./voice-input.sh start    # 启动后台守护
./voice-input.sh toggle   # 切换录音（或按 Ctrl+/）
./voice-input.sh status   # 查看状态
./voice-input.sh stop     # 停止
```

首次启动自动注册 GNOME 快捷键 `<Control>slash`。

## 文件

| 文件 | 用途 |
|------|------|
| `voice-daemon.py` | GTK4 主守护进程，包含粒子球动画 |
| `voice-input.sh` | 管理脚本，快捷键注册 |
| `stt-server.js` | faster-whisper STT HTTP 服务端 |

## 架构

```
Ctrl+/ → voice-input.sh toggle → Unix socket
                                    ↓
                            voice-daemon.py
                            ├── pw-record → WAV
                            ├── HTTP POST → stt-server.js (faster-whisper)
                            └── GTK4 orb window (Cairo particles)
```

## 许可

MIT
