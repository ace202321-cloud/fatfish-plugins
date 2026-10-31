# -*- coding: utf-8 -*-
"""肥鱼输入器 v3 主程序：照 Windows 语音输入器的悬浮胶囊 + 展开卡片。
核心（录音/ASR/翻译/剪贴板）在 fatfish_core.py，界面在 fatfish_ui.py。"""
import os
import sys
import time
import threading
import ctypes
import ctypes.wintypes as wt
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fatfish_core as core
import fatfish_ui as ui
from fatfish_ui import px

# ---------------- 原生崩溃日志（PortAudio 之类崩在 C 层也能留证据）----------------
import faulthandler as _faulthandler
try:
    _crash_fp = open(str(Path(__file__).resolve().parent / "crash.log"), "a", encoding="utf-8")
    _faulthandler.enable(file=_crash_fp, all_threads=True)
except Exception:
    pass


def _single_instance():
    """同名互斥体：已经有一个实例在跑就直接退出，避免两个实例抢热键。"""
    try:
        k = ctypes.windll.kernel32
        k.CreateMutexW(None, False, "Global\\FatFishInputSingleton")
        if k.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return False
    except Exception:
        pass
    return True

from fatfish_core import CFG, LANGS, log, save_cfg

user32 = core.user32


# ---------------------------------------------------------------- 独立性自检
import subprocess as _sp

_MARK = Path(__file__).resolve().parent / ".detached_mark"


def _in_any_job():
    """当前进程是否在某个 Job Object 里（DSH 拉起的子进程会被塞进它的 job，
    宿主一退就被一起干掉 —— 这是之前"关 DSH 语音器也没了"的根因）。"""
    try:
        k = ctypes.windll.kernel32
        res = ctypes.c_int(0)
        k.IsProcessInJob(k.GetCurrentProcess(), None, ctypes.byref(res))
        return bool(res.value)
    except Exception:
        return False


def _respawn_detached_and_exit():
    """用 WMI 重新拉起自己：WMI 创建的进程不在当前 job 里，DSH 退出也带不走。"""
    try:
        if _MARK.exists() and time.time() - _MARK.stat().st_mtime < 20:
            return False          # 刚脱离过，别再套娃
        _MARK.write_text(str(time.time()), encoding="utf-8")
        vbs = str(Path(__file__).resolve().parent / "start.vbs")
        cmd = "([wmiclass]'Win32_Process').Create('wscript.exe \"%s\"')" % vbs.replace("\\", "\\\\")
        _sp.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd],
                  creationflags=0x08000000)
        log("检测到在 Job Object 里（DSH 子进程）→ 已用 WMI 脱离并重启，本进程退出")
        return True
    except Exception as e:
        log("detach failed %r" % (e,))
        return False


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            ui.set_scale(self.root.winfo_fpixels("1i") / 96.0)
            log("DPI scale = %.2f" % (self.root.winfo_fpixels("1i") / 96.0))
        except Exception:
            pass
        self.bar = ui.Bar(self)
        self.card = None
        self.state = "idle"
        self.focus_hwnd = None
        self.recorder = core.Recorder(on_level=self._on_level, on_auto_stop=self._on_auto_stop)
        self.q = __import__("queue").Queue()

        threading.Thread(target=self._hotkeys, daemon=True).start()
        # 不在启动时预热 ASR：空闲内存从 ~340MB 降到 ~90MB，第一次说话慢 1~2 秒而已
        # threading.Thread(target=core.asr_preload, daemon=True).start()
        threading.Thread(target=core.asr_release_idle, daemon=True).start()
        self._tray()
        self.root.after(80, self._pump)

    # ---------------- 工具
    def make_noactivate(self, win):
        try:
            win.update_idletasks()
            hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
            ex = user32.GetWindowLongW(hwnd, core.GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, core.GWL_EXSTYLE,
                                  ex | core.WS_EX_NOACTIVATE | core.WS_EX_TOOLWINDOW)
        except Exception as e:
            log("noactivate %r" % (e,))

    # ---------------- 录音
    def _on_level(self, rms):
        self.bar.set_level(rms)
        if self.card:
            self.card.update_level(rms)

    def _on_auto_stop(self):
        self.q.put("stop")

    def toggle_record(self):
        log("toggle_record state=%s" % self.state)
        if self.state == "recording":
            self.q.put("stop")
            return
        if self.state == "busy":
            # 正在识别/翻译上一条：忽略，别把上一句弄丢
            log("busy：上一条还在处理，本次忽略")
            return
        self.q.put("start")

    def cfg_voice_hint(self):
        return CFG["hotkey_voice"]

    def summon_bar(self):
        """把胶囊叫回屏幕（隐藏 / 拖出屏幕 / 被盖住都拉回来）。"""
        try:
            self.bar.ensure_visible()
            self.bar.win.lift()
        except Exception as e:
            log("summon %r" % (e,))

    def start_record(self):
        try:
            self.summon_bar()
            self.bar.set_mode('full')   # 热键唤出时直接展开，看得见状态
            self.focus_hwnd = user32.GetForegroundWindow()
            self.recorder.start()
            self.state = "recording"
            self.bar.set_state("recording")
            self._close_card()
            log("录音开始")
            self.card = ui.Card(self, "正在听你说…", anchor_win=self.bar.win)
            try:
                self.bar.win.lift()
            except Exception:
                pass
            self.card.set_status("说完停一下，或点麦克风结束")
        except Exception as e:
            log("start_record %r" % (e,))
            self.state = "idle"
            self.bar.set_state("idle")
            self._toast_card("麦克风打不开：%s" % e)

    def stop_record(self):
        if self.state != "recording":
            return
        self.state = "busy"
        self.bar.set_state("busy")
        wav = core.APP_DIR / "_last.wav"
        try:
            self.recorder.stop(wav)
        except Exception as e:
            log("stop_record %r" % (e,))
            self.state = "idle"
            self.bar.set_state("idle")
            return
        if self.card:
            self.card.set_status("识别中…")
        threading.Thread(target=self._do_voice, args=(wav,), daemon=True).start()

    def _do_voice(self, wav):
        t0 = time.time()
        try:
            text = core.asr_wav(wav)
            if not text:
                self.q.put(("card_text", ("没听清", "再说一次试试？说完停一下。", None)))
                self.q.put(("idle",))
                return
            target = CFG["target_lang"]
            out = text
            if CFG.get("translate_voice", "auto") == "always" or target != "中文":
                out = core.translate_text(text, target) or text
            dt = time.time() - t0
            log("voice %.2fs -> %s" % (dt, out))
            if CFG.get("auto_paste", True):
                core.clip_set(out)
                core.paste_into(self.focus_hwnd)
                self.q.put(("card_text", ("已输入（%.1fs）" % dt, out, None, out)))
            else:
                self.q.put(("card_text", ("识别结果",
                                          "【原文】\n%s\n\n【%s】\n%s" % (text, target, out),
                                          None, out)))
            self.q.put(("idle",))
        except Exception as e:
            log("voice err %r" % (e,))
            self.q.put(("card_text", ("出错了", str(e), None)))
            self.q.put(("idle",))

    # ---------------- 截图翻译
    def shot(self):
        if self.state != "idle":
            return
        self.state = "busy"
        self.bar.set_state("busy")
        self.bar.hide()
        box = core.select_region()
        self.bar.win.deiconify()
        if not box:
            self.state = "idle"
            self.bar.set_state("idle")
            return
        self._close_card()
        self.card = ui.Card(self, "截图识别中…", anchor_win=self.bar.win)
        self.card.set_status("正在识别 + 翻译…")
        threading.Thread(target=self._do_shot, args=(box,), daemon=True).start()

    def _do_shot(self, box):
        t0 = time.time()
        try:
            png = core.grab_png(box)
            raw = core.ocr_translate_image(png, CFG["target_lang"])
            if not raw or "没有可识别" in raw:
                self.q.put(("card_text", ("没识别到文字", raw or "（空）", None)))
                self.q.put(("idle",))
                return
            # 拆出译文部分做图片
            body = raw
            if "【译文】" in raw:
                body = raw.split("【译文】", 1)[1].strip()
            path = core.make_translated_image(png, body)
            core.clip_set(raw)
            dt = time.time() - t0
            log("shot %.1fs -> %s" % (dt, path))
            self.q.put(("card_text", ("截图翻译 → %s（%.1fs）" % (CFG["target_lang"], dt),
                                      raw, path, body)))
            self.q.put(("idle",))
        except Exception as e:
            log("shot err %r" % (e,))
            self.q.put(("card_text", ("截图出错", str(e), None)))
            self.q.put(("idle",))

    # ---------------- 设置 / 帮助
    def open_settings(self):
        self._close_card()
        try:
            self._panel = ui.Panel(self, self.bar.win)
        except Exception as e:
            log("panel err %r" % (e,))

    def _toggle_paste(self):
        CFG["auto_paste"] = not CFG.get("auto_paste", True)
        save_cfg(CFG)

    def show_help(self):
        self._close_card()
        self.card = ui.Card(self, "快捷键", anchor_win=self.bar.win,
                            body="%s  开始/停止语音输入\n%s  截图翻译（拖框选区）\n%s  切换输出语言\n\n"
                                 "语音识别在本机完成，断网也能用；\n翻译需要联网（DeepSeek API），但不依赖 DSH。"
                                 % (CFG["hotkey_voice"], CFG["hotkey_shot"], CFG["hotkey_lang"]))

    # ---------------- 卡片
    def _close_card(self):
        if self.card:
            self.card.close()
            self.card = None

    def _toast_card(self, msg):
        self._close_card()
        self.card = ui.Card(self, msg, anchor_win=self.bar.win, body=msg)

    # ---------------- 消息泵
    def _pump(self):
        try:
            while True:
                cmd = self.q.get_nowait()
                try:
                    self._handle(cmd)
                except Exception as e:
                    log("pump err %r (cmd=%r)" % (e, cmd))
                    self.state = "idle"
                    self.bar.set_state("idle")
        except Exception:
            pass
        self.root.after(80, self._pump)

    def _handle(self, cmd):
        if cmd == "start":
            self.start_record()
        elif cmd == "stop":
            self.stop_record()
        elif cmd == "shot":
            self.shot()
        elif cmd == "lang":
            i = LANGS.index(CFG["target_lang"]) if CFG["target_lang"] in LANGS else 0
            CFG["target_lang"] = LANGS[(i + 1) % len(LANGS)]
            save_cfg(CFG)
            self._toast_card("输出语言：%s" % CFG["target_lang"])
        elif cmd == "hide":
            self.bar.hide()
        elif cmd == "show":
            self.bar.show()
        elif isinstance(cmd, tuple) and cmd[0] == "card_text":
            title, body, img = cmd[1][:3]
            copy_text = cmd[1][3] if len(cmd[1]) > 3 else body
            self._close_card()
            self.card = ui.Card(self, title, body=body, image_path=img,
                                anchor_win=self.bar.win, copy_text=copy_text)
            card = self.card
            self.root.after(10000, lambda: (card.close() if self.card is card else None))
        elif cmd == "idle":
            self.state = "idle"
            self.bar.set_state("idle")
            # 6 秒没动静就缩回成一个小麦克风
            self.root.after(6000, lambda: (self.bar.set_mode("mini")
                                           if self.state == "idle" else None))

    # ---------------- 热键
    def _hotkeys(self):
        hwnd = user32.CreateWindowExW(0, "STATIC", "ffv3", 0, 0, 0, 0, 0, None, None, None, None)
        mapping = [("start_stop", CFG["hotkey_voice"]), ("shot", CFG["hotkey_shot"]),
                   ("lang", CFG["hotkey_lang"])]
        MOD_NOREPEAT = 0x4000

        def parse(spec):
            mods, vk = 0, 0
            for part in str(spec).replace("+", " ").split():
                q = part.strip().lower()
                mods |= {"win": core.MOD_WIN, "shift": core.MOD_SHIFT,
                         "ctrl": core.MOD_CONTROL, "alt": core.MOD_ALT}.get(q, 0)
                if len(q) == 1:
                    vk = ord(q.upper())
            return mods | MOD_NOREPEAT, vk

        ids = {name: i + 1 for i, (name, _) in enumerate(mapping)}
        # 注册热键；失败最多重试 6 次（每 5 秒），避免"一开机被别人占了就永远没反应"
        pending = list(mapping)
        for attempt in range(6):
            still = []
            for name, spec in pending:
                mods, vk = parse(spec)
                ok = user32.RegisterHotKey(hwnd, ids[name], mods, vk)
                log("热键 %s(%s) -> %s%s" % (name, spec, "ok" if ok else "FAILED",
                                            "" if attempt == 0 else " (第%d次重试)" % attempt))
                if not ok:
                    still.append((name, spec))
            pending = still
            if not pending:
                break
            time.sleep(5)

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                        ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT)]

        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == core.WM_HOTKEY:
                if msg.wParam == 1:
                    self.q.put("start" if self.state != "recording" else "stop")
                elif msg.wParam == 2:
                    self.q.put("shot")
                elif msg.wParam == 3:
                    self.q.put("lang")

    # ---------------- 托盘
    def _tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse([4, 4, 60, 60], fill=(243, 244, 246, 255), outline=(47, 111, 237, 255), width=4)
            d.rounded_rectangle([25, 18, 39, 38], radius=7, fill=(47, 111, 237, 255))
            d.arc([20, 30, 44, 46], start=200, end=340, fill=(47, 111, 237, 255), width=3)

            def voice(i, it): self.q.put("start" if self.state != "recording" else "stop")
            def shot(i, it): self.q.put("shot")
            def lang(i, it): self.q.put("lang")
            def show(i, it): self.q.put("show")
            def hide(i, it): self.q.put("hide")
            def quit_(i, it):
                i.stop()
                os._exit(0)

            menu = pystray.Menu(
                pystray.MenuItem("🎤 开始/停止", voice),
                pystray.MenuItem("📷 截图翻译", shot),
                pystray.MenuItem("🌐 下一个语言", lang),
                pystray.MenuItem("显示胶囊", show),
                pystray.MenuItem("隐藏胶囊", hide),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", quit_),
            )
            self.tray = pystray.Icon("fatfish", img, "肥鱼输入器", menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as e:
            log("tray %r" % (e,))

    def run(self):
        log("=== v4 启动 === job=%s" % _in_any_job())
        if _in_any_job() and _respawn_detached_and_exit():
            return
        import atexit
        atexit.register(lambda: log("进程正常退出(atexit)"))
        self.root.after(120000, self._heartbeat)
        self.root.after(25000, self._asr_preload_after_start)
        self.root.after(30000, self._idle_release_mic)
        self.root.mainloop()

    def _asr_preload_after_start(self):
        """启动 25 秒后后台预热语音模型：内存够（>3.5GB 可用）才预热，否则等第一次用。"""
        def work():
            try:
                os_ok = False
                try:
                    import ctypes as _c
                    class _MS(ctypes.Structure):
                        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
                    ms = _MS(); ms.dwLength = ctypes.sizeof(_MS)
                    _c.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
                    os_ok = ms.ullAvailPhys > 3_500_000_000
                except Exception:
                    os_ok = True
                if os_ok:
                    core.asr_preload()
                    log("ASR 预热完成")
            except Exception as e:
                log("ASR 预热失败 %r" % (e,))
        threading.Thread(target=work, daemon=True).start()

    def _heartbeat(self):
        """每 2 分钟一条心跳：进程若无声消失，日志能看出大概时间点。"""
        try:
            log("心跳 state=%s" % self.state)
        except Exception:
            pass
        try:
            self.root.after(120000, self._heartbeat)
        except Exception:
            pass

    def _idle_release_mic(self):
        """每 5 分钟检查一次：闲置太久就把麦克风流关掉，释放麦克风占用。"""
        try:
            self.recorder.release_if_idle(120)
        except Exception as e:
            log("idle release err %r" % (e,))
        self.root.after(30000, self._idle_release_mic)


if __name__ == "__main__":
    # 关键：先声明 Per-Monitor V2 DPI 感知，否则 150% 缩放屏上整个界面会被系统拉伸 → 模糊
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    if not _single_instance():
        log("已有实例在运行（互斥体已存在），本进程直接退出")
        raise SystemExit(0)
    App().run()
