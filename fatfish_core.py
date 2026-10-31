# -*- coding: utf-8 -*-
"""
肥鱼输入器 v2 (FatFish Input)
================================
常驻悬浮条 + 三个全局热键，一个入口管三件事：

  Win+Shift+H   开始/停止语音输入（说完自动停；点麦克风也能停）
  Win+Shift+X   截图翻译（拖框选区 → 任意语言文字 → 目标语言）
  Win+Shift+L   循环切换输出语言（中文 / English / 日本語 / 한국어 / Русский）

v2 相对 v1 的修复（2026-10-30 用户实测反馈）：
  1. 乱码：剪贴板改用 Win32 CF_UNICODETEXT 写入。v1 走 `cmd /c clip` 喂 UTF-16LE 字节，
     被当成 ANSI 解释 → 日文变成 "S0H0~0Y0K0" 这种。现在不会再乱。
  2. 慢：HTTP 连接复用（keep-alive）、ASR 启动即预热、静音判定 700ms、点麦克风可立刻停。
  3. UI：改成常驻悬浮条（不抢焦点、可拖动、可拖动换语言、麦克风有电平动画）。
"""
import os
import sys
import json
import time
import queue
import threading
import ctypes
import ctypes.wintypes as wt
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CFG_FILE = APP_DIR / "config.json"

DEFAULTS = {
    "hotkey_voice": "Win+Shift+H",
    "hotkey_shot": "Win+Shift+X",
    "hotkey_lang": "Win+Shift+L",
    "target_lang": "中文",
    "asr_model_dir": str(APP_DIR / "models" / "asr-paraformer-zh"),
    "api_key": "",
    "api_key_file": "",
    "api_model": "deepseek-flash",
    "silence_ms": 700,
    "max_record_ms": 20000,
    "auto_paste": True,
    "translate_voice": "auto",
    "mt_engine": "auto",              # auto / online / offline
    "nllb_dir": str(APP_DIR / "models" / "nllb-200-600M"),
    "bar_x": -1,
    "bar_y": -1,
}
LANGS = ["中文", "English", "日本語", "한국어", "Русский"]
NLLB_CODE = {"中文": "zho_Hans", "English": "eng_Latn", "日本語": "jpn_Jpan",
             "한국어": "kor_Hang", "Русский": "rus_Cyrl"}



def load_cfg():
    cfg = dict(DEFAULTS)
    if CFG_FILE.exists():
        try:
            cfg.update(json.loads(CFG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    # 防御：配置被别的工具按 ANSI 读写过会出现 "涓枃" 这种乱码
    if cfg.get("target_lang") not in LANGS:
        cfg["target_lang"] = "中文"
    return cfg


def save_cfg(cfg):
    try:
        CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


CFG = load_cfg()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
WM_HOTKEY = 0x0312
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
GWL_EXSTYLE = -20
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = wt.LPVOID
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
user32.SetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
# ⚠ 64 位下必须声明返回类型，否则句柄被截成 int32 → GlobalLock 拿到 0 → 读崩
user32.GetClipboardData.restype = wt.HANDLE
user32.GetClipboardData.argtypes = [wt.UINT]
user32.OpenClipboard.argtypes = [wt.HWND]
user32.CloseClipboard.argtypes = []

LOG_FILE = APP_DIR / "fatfish_input.log"


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ 剪贴板
def clip_set(text):
    """Win32 直接写 CF_UNICODETEXT —— 中文/日文/韩文/俄文都不会乱。"""
    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise RuntimeError("GlobalAlloc 失败")
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            raise RuntimeError("SetClipboardData 失败")
    finally:
        user32.CloseClipboard()


def clip_get():
    user32.OpenClipboard(None)
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------ API（keep-alive）
import http.client
import ssl

_conn = None
_conn_lock = threading.Lock()


def read_api_key():
    """优先 config.json 的 api_key，其次 DEEPSEEK_API_KEY 环境变量，最后 api_key_file。"""
    k = (CFG.get("api_key") or "").strip()
    if k.startswith("sk-"):
        return k
    k = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if k.startswith("sk-"):
        return k
    f = CFG.get("api_key_file") or ""
    if f:
        p = Path(f)
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("sk-"):
                    return line
    return None


def api_chat(messages, max_tokens=1500, timeout=60):
    """复用同一条 HTTPS 连接：省掉每次 ~0.6s 的 TLS 握手。失败自动重连一次。"""
    global _conn
    key = read_api_key()
    if not key:
        raise RuntimeError("找不到 API key")
    payload = json.dumps({
        "model": CFG["api_model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},   # 关推理：又快又不会被思考挤空正文
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    last = None
    for attempt in range(2):
        try:
            with _conn_lock:
                if _conn is None:
                    _conn = http.client.HTTPSConnection("api.deepseek.com", timeout=timeout,
                                                        context=ssl.create_default_context())
                _conn.request("POST", "/chat/completions", body=payload, headers=headers)
                resp = _conn.getresponse()
                raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            choice = data["choices"][0]
            content = (choice["message"].get("content") or "").strip()
            if not content:
                log("空回复 finish=%s tokens=%s" % (choice.get("finish_reason"),
                                                    data.get("usage", {}).get("completion_tokens")))
            return content
        except Exception as e:
            last = e
            with _conn_lock:
                try:
                    if _conn:
                        _conn.close()
                except Exception:
                    pass
                _conn = None
            log("API 重试（%r）" % (e,))
    raise last


_local_mt = None
_local_lock = threading.Lock()


def local_mt_ready():
    try:
        d = Path(CFG["nllb_dir"])
        return (d / "model.bin").stat().st_size > 300_000_000 and (d / "sentencepiece.bpe.model").exists()
    except Exception:
        return False


def get_local_mt():
    global _local_mt
    with _local_lock:
        if _local_mt is None:
            import ctranslate2
            import sentencepiece as spm
            d = Path(CFG["nllb_dir"])
            sp = spm.SentencePieceProcessor(str(d / "sentencepiece.bpe.model"))
            tr = ctranslate2.Translator(str(d), device="cpu", compute_type="int8",
                                        inter_threads=1, intra_threads=2)
            _local_mt = (tr, sp)
            log("本地翻译模型已加载")
        return _local_mt


def translate_local(text, target):
    """NLLB-200 distilled 600M（int8 / CPU）。源语言按中文。"""
    tr, sp = get_local_mt()
    tgt = NLLB_CODE.get(target, "eng_Latn")
    tokens = ["zho_Hans"] + sp.encode(text, out_type=str)
    res = tr.translate_batch([tokens], target_prefix=[[tgt]], beam_size=4,
                             max_decoding_length=256)
    hyp = res[0].hypotheses[0]
    if hyp and hyp[0] == tgt:
        hyp = hyp[1:]
    return sp.decode(hyp).strip()


def translate_online(text, target):
    prompt = "把下面这段话翻译成%s。只输出译文，不要解释、不要引号。\n\n%s" % (target, text)
    return api_chat([{"role": "user", "content": prompt}], max_tokens=1200)


def translate_text(text, target):
    """按设置选引擎：auto=本地可用就本地（快、免费），否则在线；可强制 online/offline。"""
    engine = CFG.get("mt_engine", "auto")
    if engine in ("auto", "offline") and local_mt_ready():
        try:
            t0 = time.time()
            out = translate_local(text, target)
            log("本地翻译 %.2fs" % (time.time() - t0))
            if out:
                return out
        except Exception as e:
            log("本地翻译失败 %r → 回退在线" % (e,))
            if engine == "offline":
                raise
    if engine == "offline":
        raise RuntimeError("离线翻译模型还没就绪（%s）" % CFG["nllb_dir"])
    return translate_online(text, target)


def ocr_translate_image(png_bytes, target):
    import base64
    b64 = base64.b64encode(png_bytes).decode()
    prompt = ("这是一张屏幕截图。请：\n"
              "1) 找出图中所有可见文字（可能中/英/日/韩/俄等），按阅读顺序原样抄下来；\n"
              "2) 把这些文字翻译成%s。\n"
              "输出格式：\n【原文】\n...\n【译文】\n...\n"
              "如果图中没有文字，只回答：图中没有可识别的文字。" % target)
    return api_chat([{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
    ]}], max_tokens=4000)


# ------------------------------------------------------------------ ASR（启动即预热）
_asr = None
_asr_lock = threading.Lock()
_asr_last = 0.0


def get_asr():
    global _asr, _asr_last
    with _asr_lock:
        if _asr is None:
            import sherpa_onnx
            d = Path(CFG["asr_model_dir"])
            if not (d / "model.int8.onnx").exists():
                # 兜底：模型可能被别人挪走，按常见位置再找一遍
                for cand in (APP_DIR / "models" / "asr-paraformer-zh",
                             Path.home() / ".dsh" / "toolkits" / "video-kit" /
                             "models" / "asr-paraformer-zh"):
                    if (cand / "model.int8.onnx").exists():
                        d = cand
                        break
            _asr = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(d / "model.int8.onnx"), tokens=str(d / "tokens.txt"),
                num_threads=2, sample_rate=16000, feature_dim=80,
                decoding_method="greedy_search")
            log("ASR 已加载")
        _asr_last = time.time()
        return _asr


def asr_preload():
    try:
        get_asr()
    except Exception as e:
        log("ASR 预热失败 %r" % (e,))


def asr_release_idle():
    """空闲 10 分钟释放模型（打游戏时省内存）"""
    global _asr
    while True:
        time.sleep(60)
        if _asr is not None and time.time() - _asr_last > 600:
            with _asr_lock:
                _asr = None
            import gc
            gc.collect()
            log("ASR 已释放（空闲）")


def asr_wav(path):
    import wave
    import numpy as np
    rec = get_asr()
    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    s = rec.create_stream()
    s.accept_waveform(16000, data)
    rec.decode_stream(s)
    return (s.result.text or "").strip()


# ------------------------------------------------------------------ 录音（可主动停止 + 电平）
class Recorder:
    def __init__(self, on_level=None, on_auto_stop=None):
        self.stream = None
        self.chunks = []
        self.on_level = on_level
        self.on_auto_stop = on_auto_stop
        self.stop_flag = threading.Event()
        self.started_at = 0
        self.spoke = False
        self.silence_blocks = 0
        self._lock = threading.Lock()

    def start(self):
        import sounddevice as sd
        self.chunks = []
        self.stop_flag.clear()
        self.started_at = time.time()
        self.spoke = False
        self.silence_blocks = 0

        def cb(indata, frames, t, status):
            with self._lock:
                self.chunks.append(indata.copy())
            import numpy as np
            rms = float(np.sqrt(np.mean(indata ** 2)))
            if self.on_level:
                self.on_level(rms)
            if rms > 0.012:
                self.spoke = True
                self.silence_blocks = 0
            else:
                self.silence_blocks += 1
                sil_ms = self.silence_blocks * 100
                dur_ms = (time.time() - self.started_at) * 1000
                if (self.spoke and sil_ms >= CFG["silence_ms"]) or dur_ms >= CFG["max_record_ms"]:
                    if self.on_auto_stop:
                        self.on_auto_stop()

        self.stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                                     blocksize=1600, callback=cb)
        self.stream.start()

    def stop(self, out_wav):
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        finally:
            self.stream = None
        import numpy as np
        import wave
        with self._lock:
            chunks = list(self.chunks)
        audio = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 1), dtype="float32")
        with wave.open(str(out_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((audio * 32767).astype(np.int16).tobytes())
        return out_wav


# ------------------------------------------------------------------ 粘贴
def paste_into(hwnd):
    try:
        if hwnd:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.12)
    except Exception:
        pass
    VK_CONTROL, VK_V = 0x11, 0x56
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 2, 0)
    user32.keybd_event(VK_CONTROL, 0, 2, 0)


# ------------------------------------------------------------------ 截图
def select_region(stop_evt=None):
    import tkinter as tk
    result = {}
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.28)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    canvas = tk.Canvas(root, cursor="cross", bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    st = {"x": 0, "y": 0, "rect": None}

    def press(e):
        st["x"], st["y"] = e.x_root, e.y_root
        st["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ef4444", width=3)

    def drag(e):
        if st["rect"]:
            canvas.coords(st["rect"], st["x"] - root.winfo_rootx(),
                          st["y"] - root.winfo_rooty(), e.x, e.y)

    def release(e):
        x1, y1, x2, y2 = st["x"], st["y"], e.x_root, e.y_root
        result["box"] = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        root.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    root.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()
    box = result.get("box")
    return box if box and box[2] - box[0] > 8 and box[3] - box[1] > 8 else None


def grab_png(box):
    from PIL import ImageGrab
    import io
    img = ImageGrab.grab(bbox=box, all_screens=False)
    if img.width > 1600:
        r = 1600 / img.width
        img = img.resize((1600, int(img.height * r)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ------------------------------------------------------------------ 译文图片
SHOT_DIR = APP_DIR / "shots"
SHOT_DIR.mkdir(exist_ok=True)


def make_translated_image(png_bytes, translated_text):
    """把原截图 + 译文排版成一张对照图，返回保存路径。"""
    from PIL import Image, ImageDraw, ImageFont
    import io

    orig = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if orig.width > 1200:
        r = 1200 / orig.width
        orig = orig.resize((1200, int(orig.height * r)), Image.LANCZOS)

    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 22)
    title_font = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 26)

    # 文本换行
    tmp = Image.new("RGB", (10, 10))
    td = ImageDraw.Draw(tmp)
    max_w = orig.width - 56
    lines = []
    for raw in translated_text.splitlines() or [translated_text]:
        cur = ""
        for ch in raw:
            if td.textlength(cur + ch, font=font) > max_w:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)

    line_h = 34
    pad = 28
    head_h = 56
    text_h = max(1, len(lines)) * line_h
    out = Image.new("RGB", (orig.width, orig.height + head_h + text_h + pad * 2), (250, 250, 252))
    out.paste(orig, (0, 0))
    d = ImageDraw.Draw(out)
    y = orig.height + 18
    d.text((pad, y), "译文", font=title_font, fill=(47, 111, 237))
    y += head_h - 8
    for ln in lines:
        d.text((pad, y), ln, font=font, fill=(31, 35, 40))
        y += line_h

    path = SHOT_DIR / ("shot_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    out.save(path)
    return str(path)
