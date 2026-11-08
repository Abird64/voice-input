#!/usr/bin/env python3
"""
语音输入守护 — GTK4 + Cairo VoiceOrbs 风格 (v6)
弃用窗口透明，改为深色卡片 + Cairo 30fps 动画
"""

import os, sys, socket, json, threading, time, subprocess, urllib.request, urllib.error, math
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('GLib', '2.0')
gi.require_version('cairo', '1.0')
from gi.repository import Gtk, Gdk, GLib
import cairo

RUNTIME_DIR = f'/run/user/{os.getuid()}/voice-input'
SOCK_PATH   = os.path.join(RUNTIME_DIR, 'daemon.sock')
STT_URL     = 'http://127.0.0.1:9876'
LOG_FILE    = os.path.join(RUNTIME_DIR, 'daemon.log')

def get_token():
    try:
        with open(os.path.join(RUNTIME_DIR, 'token')) as f:
            return f.read().strip()
    except: return 'voice-input-local'
STT_TOKEN = get_token()

def log(msg):
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(f'[{time.strftime("%H:%M:%S")}] {msg}\n')
    except: pass

# ── 录音 ──
class Recorder:
    def __init__(self):
        self.proc = None
        self.path = os.path.join(RUNTIME_DIR, 'current.wav')
    def start(self):
        try: os.unlink(self.path)
        except: pass
        self.proc = subprocess.Popen(
            ['pw-record', '--rate', '16000', '--channels', '1', '--format', 's16', self.path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def stop(self):
        if self.proc:
            self.proc.terminate()
            try: self.proc.wait(timeout=2)
            except: self.proc.kill()
            self.proc = None
    def get_wav(self):
        try:
            with open(self.path, 'rb') as f: d = f.read()
            return d if len(d) > 44 else None
        except: return None

# ── STT ──
def transcribe(wav):
    req = urllib.request.Request(f'{STT_URL}/transcribe?language=zh', data=wav,
        headers={'Authorization': f'Bearer {STT_TOKEN}', 'Content-Type': 'audio/wav'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get('text', '').strip()
    except Exception as e:
        log(f'STT err: {e}')
        return None

# ═══════════════════════════════════════════════════════════
#  Particles Orb — Cairo 移植
#  核心: Fibonacci 球面粒子 + 状态驱动变形 + additive blending
# ═══════════════════════════════════════════════════════════

CANVAS_SIZE = 64
SCALE       = 2                     # 渲染分辨率 128×128
PARTICLES   = 300                   # 粒子数
TONE_BINS   = 5                     # 色调分桶
ALPHA_BINS  = 6                     # 透明度分桶
BUCKETS     = TONE_BINS * ALPHA_BINS
TWO_PI      = 2.0 * math.pi
GOLDEN_ANG  = math.pi * (3.0 - math.sqrt(5.0))
ANGLE_X     = 0.32                  # 固定 X 轴倾斜

# ── 预计算 Fibonacci 球面 ──
_sphere_x  = [0.0] * PARTICLES
_sphere_y  = [0.0] * PARTICLES
_sphere_z  = [0.0] * PARTICLES
_sphere_rf = [0.0] * PARTICLES  # ring fraction
_sphere_sd = [0.0] * PARTICLES  # random seed
_sphere_tn = [0]   * PARTICLES  # tone bin

for _i in range(PARTICLES):
    yy = 1.0 - (_i / (PARTICLES - 1)) * 2.0
    rad = math.sqrt(1.0 - yy * yy)
    th  = GOLDEN_ANG * _i
    _sphere_x[_i]  = math.cos(th) * rad
    _sphere_y[_i]  = yy
    _sphere_z[_i]  = math.sin(th) * rad
    _sphere_rf[_i] = (_i * 0.61803398875) % 1.0
    _sphere_sd[_i] = ((_i * 0.7548776662) % 1.0) * TWO_PI
    _sphere_tn[_i] = min(TONE_BINS - 1, int(((_i * 0.5436890126) % 1.0) * TONE_BINS))

# ── 状态参数表 ──
class SP: pass  # namespace

SP.idle       = dict(tempo=1.0, spin=0.14, breathe=0.05, drift=1.0, ripple=0,    swell=0.04, flow=0,   swirl=0,   pulse=0,    pulseRate=1.0, ring=0,   jitter=0,   shake=0,   alpha=0.72, rest=0)
SP.listening  = dict(tempo=1.0, spin=0.55, breathe=0.012,drift=0,   ripple=1.0, swell=0.12, flow=0,   swirl=0,   pulse=0,    pulseRate=1.0, ring=0,   jitter=0,   shake=0,   alpha=0.92, rest=0.55)
SP.thinking   = dict(tempo=1.0, spin=0.32, breathe=0.01, drift=0,   ripple=0,   swell=0,    flow=0,   swirl=0,   pulse=1.0,  pulseRate=1.0, ring=0,   jitter=0,   shake=0,   alpha=0.78, rest=0.3)

# 逐帧缓存 (避免每帧分配)
_px = [0.0] * PARTICLES
_py = [0.0] * PARTICLES
_pr = [0.0] * PARTICLES
_pbucket  = [0] * PARTICLES
_counts   = [0] * BUCKETS
_starts   = [0] * BUCKETS
_order    = [0] * PARTICLES

def _paint_particles(cr, w, h, t, p):
    """单帧 particles orb 绘制
       cr: 已缩放至逻辑像素的 Cairo context
       p:  状态参数字典 (如 SP.listening)
    """
    cx = w / 2.0
    base_r = cx * 0.70           # 球体基础半径
    n = PARTICLES
    clock = t + 1.7
    pt    = t + 1.7              # pulse clock
    beat  = math.sin(pt * 2.6) * 0.5 + 0.5
    beat3 = beat * beat * beat

    radius = base_r * (1.0 + p['breathe'] * math.sin(clock * 1.1)
                       + p['swell'] * 0.0   # no live audio level
                       - p['pulse'] * (0.06 + 0.12 * beat3))

    angle_y = t * p['spin'] * (1.0 + p['ripple'] * 0.0 * 1.8)
    cos_y, sin_y = math.cos(angle_y), math.sin(angle_y)
    cos_x, sin_x = math.cos(ANGLE_X), math.sin(ANGLE_X)

    ripple = p['ripple'] * (0.04 + 0.0 * 0.22)
    pulse_a = p['pulse'] * 0.16 * (0.4 + 0.6 * beat)
    flow_a  = p['flow'] * (0.18 + 0.0 * 0.4)
    swirl_a = p['swirl'] * (0.35 + 0.0 * 0.9)

    for i in range(BUCKETS):
        _counts[i] = 0

    for i in range(n):
        sx, sy, sz = _sphere_x[i], _sphere_y[i], _sphere_z[i]

        # 绕 Y 旋转 + swirl
        twist = angle_y if swirl_a < 0.002 else angle_y + swirl_a * math.sin(sy * 2.4 + t * 1.6)
        cy2, sny2 = math.cos(twist), math.sin(twist)
        x1 = sx * cy2 - sz * sny2
        z1 = sx * sny2 + sz * cy2
        y1 = sy * cos_x - z1 * sin_x

        depth = (z1 + 1.0) / 2.0
        persp = 0.65 + depth * 0.45
        pr = radius

        if ripple > 0.002:
            pr *= 1.0 + ripple * (0.5 + 0.5 * math.sin(sy * 4.5 - t * 6.5))
        if pulse_a > 0.002:
            pr *= 1.0 - pulse_a * (0.5 + 0.5 * math.sin(_sphere_rf[i] * TWO_PI + pt * 3.1))
        if flow_a > 0.002:
            stream = 0.5 + 0.5 * math.sin(_sphere_sd[i] * 3.0 - t * 3.4)
            pr *= 1.0 - flow_a * stream * stream

        screen_x = cx + x1 * pr * persp
        screen_y = cx + y1 * pr * persp
        alpha = 0.12 + depth * depth * 0.78
        dot_r = 0.6 + depth * 1.5

        _px[i] = screen_x
        _py[i] = screen_y
        _pr[i] = dot_r * 0.7  # scale dot
        ab = min(ALPHA_BINS - 1, int(alpha * ALPHA_BINS))
        b = _sphere_tn[i] * ALPHA_BINS + ab
        _pbucket[i] = b
        _counts[b] += 1

    # 桶排序
    acc = 0
    for b in range(BUCKETS):
        _starts[b] = acc
        acc += _counts[b]
    for i in range(n):
        b = _pbucket[i]
        pos = _starts[b]
        _order[pos] = i
        _starts[b] = pos + 1

    # ── 绘制 ──
    cr.save()
    cr.set_operator(cairo.Operator.CLEAR)
    cr.paint()
    cr.restore()

    # 底暗圆 (OVER)
    cr.set_source_rgba(0.07, 0.08, 0.12, 0.92)
    cr.arc(cx, cx, base_r * 1.08, 0, TWO_PI)
    cr.fill()

    # additive 粒子
    cr.set_operator(cairo.Operator.ADD)

    cursor = 0
    # 颜色: listening=蓝青, thinking=紫蓝
    if p is SP.listening:
        c0r, c0g, c0b = 0.30, 0.55, 1.00
        c1r, c1g, c1b = 0.08, 0.72, 0.90
    elif p is SP.thinking:
        c0r, c0g, c0b = 0.90, 0.55, 1.00
        c1r, c1g, c1b = 0.50, 0.55, 1.00
    else:
        c0r, c0g, c0b = 0.60, 0.60, 0.70
        c1r, c1g, c1b = 0.40, 0.40, 0.50

    for b in range(BUCKETS):
        cnt = _counts[b]
        if cnt == 0:
            continue
        ab = b % ALPHA_BINS
        tb = (b - ab) // ALPHA_BINS
        frac = (tb + 0.5) / TONE_BINS
        r = (1.0 - frac) * c0r + frac * c1r
        g = (1.0 - frac) * c0g + frac * c1g
        bv = (1.0 - frac) * c0b + frac * c1b
        bucket_alpha = ((ab + 0.5) / ALPHA_BINS) * p['alpha'] * 0.55
        cr.set_source_rgba(r, g, bv, bucket_alpha)

        cr.new_path()
        for k in range(cursor, cursor + cnt):
            i = _order[k]
            cr.move_to(_px[i] + _pr[i], _py[i])
            cr.arc(_px[i], _py[i], _pr[i], 0, TWO_PI)
        cr.fill()
        cursor += cnt

    cr.set_operator(cairo.Operator.OVER)

class OrbWidget(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(False)
        self._picture.set_size_request(CANVAS_SIZE, CANVAS_SIZE)
        self.append(self._picture)

        self._frame   = 0
        self._mode    = 'idle'
        self._anim_id = 0

        pw = CANVAS_SIZE * SCALE
        self._surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, pw)
        self._cr      = cairo.Context(self._surface)
        self._paint_frame(0.0)
        self._upload()

    def _upload(self):
        w = CANVAS_SIZE * SCALE
        stride = self._surface.get_stride()
        buf    = self._surface.get_data()
        tex = Gdk.MemoryTexture.new(w, w, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                    GLib.Bytes(buf.tobytes()), stride)
        self._picture.set_paintable(tex)

    def set_mode(self, mode):
        self._mode = mode
        if mode in ('recording', 'processing'):
            self._start_anim()
        else:
            self._stop_anim()

    def _start_anim(self):
        if self._anim_id:
            return
        def tick():
            self._frame += 1
            t = self._frame * 0.033
            self._paint_frame(t)
            self._upload()
            return True
        self._anim_id = GLib.timeout_add(33, tick)

    def _stop_anim(self):
        if self._anim_id:
            GLib.source_remove(self._anim_id)
            self._anim_id = 0
            self._frame = 0
            self._paint_frame(0.0)
            self._upload()

    def _paint_frame(self, t):
        cr = self._cr
        cr.save()
        cr.scale(SCALE, SCALE)
        if self._mode == 'recording':
            _paint_particles(cr, CANVAS_SIZE, CANVAS_SIZE, t, SP.listening)
        elif self._mode == 'processing':
            _paint_particles(cr, CANVAS_SIZE, CANVAS_SIZE, t, SP.thinking)
        else:
            _paint_particles(cr, CANVAS_SIZE, CANVAS_SIZE, t, SP.idle)
        cr.restore()

# ═══════════════════════════════════════════════════════════
#  Orb 窗口 — 深色卡片内嵌 Cairo 画布
# ═══════════════════════════════════════════════════════════

class OrbWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_decorated(False); self.set_resizable(False)
        self.set_title('VoiceOrb')
        self.set_default_size(88, 88)
        self.set_css_classes(['orb-window'])

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.set_css_classes(['orb-card'])
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)
        card.set_margin_start(8); card.set_margin_end(8)
        card.set_margin_top(8);   card.set_margin_bottom(8)
        self.set_child(card)

        self.canvas = OrbWidget()
        card.append(self.canvas)

        self.set_visible(False)

    def show_recording(self):
        self.canvas.set_mode('recording')
        if not self.is_visible():
            self.set_visible(True)

    def show_processing(self):
        self.canvas.set_mode('processing')
        if not self.is_visible():
            self.set_visible(True)

    def hide_window(self):
        self.canvas.set_mode('idle')
        self.set_visible(False)

# ═══════════════════════════════════════════════════════════
#  结果窗口
# ═══════════════════════════════════════════════════════════

class ResultWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_decorated(False); self.set_resizable(False)
        self.set_title('识别结果')
        self.set_css_classes(['result-window'])

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_css_classes(['result-window-bg'])
        outer.set_margin_start(20); outer.set_margin_end(20)
        outer.set_margin_top(18); outer.set_margin_bottom(18)
        self.set_child(outer)

        # 标题行 — 小圆点 + 文字
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        dot = Gtk.DrawingArea()
        dot.set_content_width(10); dot.set_content_height(10)
        dot.set_css_classes(['result-dot'])
        dot.set_draw_func(lambda a, cr, w, h, _: (
            cr.set_source_rgba(0.4, 0.8, 0.5, 1.0),
            cr.arc(w/2, h/2, 4, 0, 2*math.pi),
            cr.fill()
        ))
        hdr.append(dot)
        title = Gtk.Label(label='识别完成')
        title.set_css_classes(['result-title'])
        hdr.append(title)
        outer.append(hdr)

        self.textview = Gtk.TextView()
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_editable(False); self.textview.set_cursor_visible(True)
        self.textview.set_css_classes(['textview'])
        self.textview.set_size_request(460, -1)
        outer.append(self.textview)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        copy_btn = Gtk.Button(label='复制')
        copy_btn.set_css_classes(['btn-copy'])
        copy_btn.connect('clicked', self._copy_and_close)
        btns.append(copy_btn)
        close_btn = Gtk.Button(label='关闭')
        close_btn.set_css_classes(['btn-close'])
        close_btn.connect('clicked', lambda _: self.hide())
        btns.append(close_btn)
        outer.append(btns)
        self.set_visible(False)

    def _copy_and_close(self, btn):
        buf = self.textview.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if text:
            Gdk.Display.get_default().get_clipboard().set(text)
            log(f'copy: "{text[:30]}"')
        self.hide()

    def show_result(self, text):
        buf = self.textview.get_buffer()
        buf.set_text(text, -1)
        self.present()
        GLib.timeout_add(200, self._select_all)

    def _select_all(self):
        self.textview.grab_focus()
        buf = self.textview.get_buffer()
        buf.select_range(buf.get_start_iter(), buf.get_end_iter())
        return False

# ═══════════════════════════════════════════════════════════
#  CSS
# ═══════════════════════════════════════════════════════════

CSS = b"""
window.orb-window {
    background-color: transparent;
    box-shadow: none;
}
.orb-card {
    background: rgba(16, 17, 22, 0.97);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 999px;
}

/* Result window */
window.result-window {
    background-color: transparent;
    box-shadow: none;
}
.result-window-bg {
    background: rgba(24,24,27,0.96);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 16px;
}
.result-window-bg textview {
    background: rgba(255,255,255,0.05);
    color: #e4e4e7;
    border-radius: 10px;
    padding: 10px;
    font-size: 15px;
}
.result-window-bg textview selection {
    background: rgba(0,180,216,0.45);
}
.result-window-bg .result-title {
    color: rgba(255,255,255,0.55);
    font-size: 13px;
    font-weight: 500;
}
.result-dot {
    margin-top: 2px;
}
.result-window-bg .btn-copy {
    background: rgba(120,140,255,0.18);
    color: rgba(200,210,255,0.95);
    border-radius: 8px;
    padding: 6px 20px;
    font-size: 13px;
    font-weight: 600;
}
.result-window-bg .btn-copy:hover {
    background: rgba(120,140,255,0.30);
}
.result-window-bg .btn-close {
    background: rgba(255,255,255,0.06);
    color: rgba(255,255,255,0.50);
    border-radius: 8px;
    padding: 6px 18px;
    font-size: 13px;
}
.result-window-bg .btn-close:hover {
    background: rgba(255,255,255,0.12);
}
"""

# ═══════════════════════════════════════════════════════════
#  守护进程
# ═══════════════════════════════════════════════════════════

class Daemon(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='com.abird.VoiceInput')
        self.rec = Recorder()
        self.orb = None
        self.result = None
        self.state = 'idle'

    def do_activate(self):
        log('daemon activate')
        p = Gtk.CssProvider(); p.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.orb = OrbWindow(self)
        self.result = ResultWindow(self)
        self._setup_socket()

    def _setup_socket(self):
        try: os.unlink(SOCK_PATH)
        except: pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        self.sock.listen(5); self.sock.setblocking(False)
        GLib.io_add_watch(self.sock.fileno(), GLib.IOCondition.IN, self._on_ipc)
        log('sock ok')

    def _on_ipc(self, fd, cond):
        try:
            conn, _ = self.sock.accept(); data = conn.recv(1024).decode().strip(); conn.close()
        except: return True
        log(f'ipc: "{data}" {self.state}')
        if data == 'toggle': self._toggle()
        elif data == 'quit': self._shutdown()
        return True

    def _toggle(self):
        if self.state == 'idle':
            self.state = 'recording'
            self.rec.start()
            GLib.idle_add(self.orb.show_recording)
        elif self.state == 'recording':
            self.state = 'processing'
            self.rec.stop()
            GLib.idle_add(self.orb.show_processing)
            threading.Thread(target=self._process, daemon=True).start()

    def _process(self):
        time.sleep(0.3)
        wav = self.rec.get_wav()
        text = transcribe(wav) if wav else None
        GLib.idle_add(self.orb.hide_window)
        if text:
            GLib.idle_add(self.result.show_result, text)
        GLib.idle_add(self._go_idle)

    def _go_idle(self):
        self.state = 'idle'; return False
    def _shutdown(self):
        if self.rec.proc: self.rec.stop()
        try: os.unlink(SOCK_PATH)
        except: pass
        self.quit()
    def do_shutdown(self):
        if self.rec.proc: self.rec.stop()
        try: os.unlink(SOCK_PATH)
        except: pass
        Gtk.Application.do_shutdown(self)

if __name__ == '__main__':
    Daemon().run()