# -*- coding: utf-8 -*-
"""肥鱼输入器 v4 —— Fluent 风格 UI（照 Windows 11 语音输入器）
要点：
  · 进程先开 Per-Monitor DPI 感知，所有尺寸按真实 DPI 放大 → 不再模糊
  · 文字用负数字号（像素），图标用 Segoe Fluent Icons（Win11 自带）
  · 设置面板按 Fluent 规范：白卡 8px 圆角、1px 描边、44px 行高、开关是蓝底圆头滑块
"""
import os
import time
import tkinter as tk
from pathlib import Path

ICON_FONT = "Segoe Fluent Icons"
UI_FONT = "Segoe UI"
CN_FONT = "Microsoft YaHei UI"

# Fluent 配色（Win11 浅色）
C_SURFACE = "#FFFFFF"
C_BORDER = "#E5E5E5"
C_TEXT = "#1A1A1A"
C_TEXT2 = "#5D5D5D"
C_TEXT3 = "#8A8A8A"
C_HOVER = "#F3F3F3"
C_ACCENT = "#005FB8"
C_TRACK_OFF = "#BDBDBD"

GLYPH_GEAR = "\uE713"
GLYPH_MIC = "\uE720"
GLYPH_HELP = "\uE897"
GLYPH_CLOSE = "\uE8BB"
GLYPH_CAMERA = "\uE722"
GLYPH_CHECK = "\uE73E"
GLYPH_CHEVRON = "\uE76C"
GLYPH_GLOBE = "\uE774"

S = 1.0


def set_scale(s):
    global S
    S = max(1.0, float(s))


def px(n):
    return int(round(n * S))


def fnt(size_px, weight="normal", family=CN_FONT):
    return (family, -px(size_px), weight)


SS = 4  # 超采样倍数：先在 4 倍尺寸画，再 LANCZOS 缩回去，圆角/描边才不毛


def rounded(w, h, r, fill, outline=None, width=1):
    from PIL import Image, ImageDraw, ImageTk
    W, H, R = max(2, w * SS), max(2, h * SS), max(1, r * SS)
    big = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=R, fill=fill,
                        outline=outline, width=max(1, width * SS))
    img = big.resize((max(2, w), max(2, h)), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


class Toggle(tk.Canvas):
    def __init__(self, master, value=True, command=None, bg=C_SURFACE):
        self.w, self.h = px(40), px(20)
        super().__init__(master, width=self.w, height=self.h, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.value = bool(value)
        self.command = command
        self.bind("<Button-1>", self._toggle)
        self._draw()

    def _draw(self):
        self.delete("all")
        r = self.h // 2
        track = C_ACCENT if self.value else C_TRACK_OFF
        self.create_oval(0, 0, self.h, self.h, fill=track, outline="")
        self.create_oval(self.w - self.h, 0, self.w, self.h, fill=track, outline="")
        self.create_rectangle(self.h // 2, 0, self.w - self.h // 2, self.h, fill=track, outline="")
        pad = px(3)
        knob = self.h - pad * 2
        cx = (self.w - self.h + pad) if self.value else pad
        self.create_oval(cx, pad, cx + knob, pad + knob, fill="#FFFFFF", outline="")

    def _toggle(self, _e):
        self.value = not self.value
        self._draw()
        if self.command:
            self.command(self.value)


class Bar:
    """常驻胶囊：mini（一个小麦克风圆钮）/ full（齿轮 + 麦克风 + 问号）。

    实现要点：
      · 两个独立 Toplevel（mini / full），切换只做 withdraw/deiconify —— 避免运行时改窗口大小
        导致 Canvas 背景露黑（上一版就是栽在这）。
      · 每个窗口的背景/图标都用 PIL 4 倍超采样渲染，边缘不糊。
    """
    W_FULL, H_FULL = 204, 60
    D_MINI = 56
    ICON_TTF = r"C:\Windows\Fonts\SegoeIcons.ttf"

    def __init__(self, app):
        self.app = app
        self.mode = "mini"
        self.state = "idle"
        self.level = 0.0
        self._zone = None
        self._moved = False
        self._win = {}
        self._canvas = {}
        self._photo = {}
        for mode in ("mini", "full"):
            w, h = self._size_of(mode)
            win = tk.Toplevel(app.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg="#0a0f1e")
            try:
                win.attributes("-transparentcolor", "#0a0f1e")
            except Exception:
                pass
            cv = tk.Canvas(win, width=w, height=h, bg="#0a0f1e", highlightthickness=0)
            cv.pack()
            self._win[mode] = win
            self._canvas[mode] = cv
            self._bind_events(win, cv, mode)
        self._render_all()
        self._place()
        self._apply_mode()

    def _size_of(self, mode):
        return (px(self.D_MINI), px(self.D_MINI)) if mode == "mini" else (px(self.W_FULL), px(self.H_FULL))

    # ---------- 渲染
    def _make_image(self, mode):
        from PIL import Image, ImageDraw, ImageFont
        w, h = self._size_of(mode)
        W, H = w * SS, h * SS
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        rec = self.state == "recording"
        busy = self.state == "busy"
        rad = (H // 2) if mode == "mini" else int(px(30) * SS)
        d.rounded_rectangle([0, 0, W - 1, H - 1], radius=rad, fill=C_SURFACE,
                            outline=C_BORDER, width=max(1, int(px(1) * SS)))

        def icon(ch, size_px, color, cx, cy):
            f = ImageFont.truetype(self.ICON_TTF, int(size_px * SS))
            bb = d.textbbox((0, 0), ch, font=f)
            d.text((cx * SS - (bb[2] + bb[0]) / 2, cy * SS - (bb[3] + bb[1]) / 2),
                   ch, font=f, fill=color)

        if mode == "mini":
            cx = cy = w / 2
            if rec:
                lvl = min(1.0, self.level * 26)
                rr = (w / 2 - px(3)) + px(3) * lvl
                d.ellipse([(cx - rr) * SS, (cy - rr) * SS, (cx + rr) * SS, (cy + rr) * SS],
                          outline="#E5484D", width=max(2, int(px(2) * SS)))
            fill = "#E5484D" if rec else ("#005FB8" if busy else C_HOVER)
            r = w / 2 - px(6)
            d.ellipse([(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS], fill=fill)
            icon(GLYPH_MIC, 20, "#FFFFFF" if (rec or busy) else C_TEXT, cx, cy)
        else:
            cy = h / 2
            icon(GLYPH_GEAR, 17, C_TEXT2, px(30), cy)
            icon(GLYPH_HELP, 17, C_TEXT2, w - px(30), cy)
            cx = w / 2
            if rec:
                lvl = min(1.0, self.level * 26)
                rr = px(19) + px(4) + px(7) * lvl
                d.ellipse([(cx - rr) * SS, (cy - rr) * SS, (cx + rr) * SS, (cy + rr) * SS],
                          outline="#E5484D", width=max(2, int(px(2) * SS)))
            fill = "#E5484D" if rec else ("#005FB8" if busy else C_HOVER)
            r = px(19)
            d.ellipse([(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS], fill=fill)
            icon(GLYPH_MIC, 17, "#FFFFFF" if (rec or busy) else C_TEXT, cx, cy)

        img = img.resize((w, h), Image.LANCZOS)
        from PIL import ImageTk
        return ImageTk.PhotoImage(img)

    def _render_all(self):
        for mode in ("mini", "full"):
            self._photo[mode] = self._make_image(mode)
            cv = self._canvas[mode]
            cv.delete("all")
            cv.create_image(0, 0, image=self._photo[mode], anchor="nw")

    # ---------- 模式
    def set_mode(self, mode):
        if mode == self.mode:
            return
        old = self._win[self.mode]
        x, y = old.winfo_x(), old.winfo_y()
        self.mode = mode
        w, h = self._size_of(mode)
        sw, sh = self._win[mode].winfo_screenwidth(), self._win[mode].winfo_screenheight()
        x = max(px(4), min(sw - w - px(4), x))
        y = max(px(4), min(sh - h - px(4), y))
        self._win[mode].geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._apply_mode()

    def _apply_mode(self):
        for mode in ("mini", "full"):
            if mode == self.mode:
                self._win[mode].deiconify()
                self._win[mode].attributes("-topmost", True)
                self._win[mode].lift()
                self.app.make_noactivate(self._win[mode])
            else:
                self._win[mode].withdraw()

    # ---------- 交互
    def _bind_events(self, win, cv, mode):
        cv.bind("<ButtonPress-1>", lambda e: self._press(e, mode))
        cv.bind("<B1-Motion>", self._move)
        cv.bind("<ButtonRelease-1>", lambda e: self._release(e, mode))
        cv.bind("<Button-3>", self._context_menu)

    def _press(self, e, mode):
        w, _ = self._size_of(mode)
        if mode == "mini":
            self._zone = "mic"
        else:
            self._zone = "gear" if e.x < w * 0.3 else ("help" if e.x > w * 0.7 else "mic")
        self._dx = e.x_root - self._win[mode].winfo_x()
        self._dy = e.y_root - self._win[mode].winfo_y()
        self._moved = False

    def _move(self, e):
        self._moved = True
        self._win[self.mode].geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))

    def _release(self, e, mode):
        if self._moved:
            from fatfish_core import CFG, save_cfg
            CFG["bar_x"], CFG["bar_y"] = self._win[mode].winfo_x(), self._win[mode].winfo_y()
            save_cfg(CFG)
            self._moved = False
            return
        if self._zone == "mic":
            if mode == "mini":
                self.set_mode("full")
            self.app.toggle_record()
        elif self._zone == "gear":
            self.app.open_settings()
        elif self._zone == "help":
            self.app.show_help()

    def _context_menu(self, e):
        m = tk.Menu(self._win[self.mode], tearoff=0)
        m.add_command(label="设置…", command=self.app.open_settings)
        m.add_command(label="截图翻译", command=self.app.shot)
        m.add_separator()
        m.add_command(label="收起成小麦克风", command=lambda: self.set_mode("mini"))
        m.add_command(label="展开胶囊", command=lambda: self.set_mode("full"))
        m.add_command(label="隐藏（%s 可叫回）" % self.app.cfg_voice_hint(),
                      command=self._win[self.mode].withdraw)
        m.add_command(label="退出", command=lambda: __import__("os")._exit(0))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _place(self):
        sw = self._win["mini"].winfo_screenwidth()
        sh = self._win["mini"].winfo_screenheight()
        from fatfish_core import CFG
        x, y = CFG.get("bar_x", -1), CFG.get("bar_y", -1)
        if x is None or x < 0 or y is None or y < 0:
            w, h = self._size_of("mini")
            x, y = (sw - w) // 2, sh - h - px(80)
        for mode in ("mini", "full"):
            w, h = self._size_of(mode)
            self._win[mode].geometry("%dx%d+%d+%d" % (w, h,
                                                     min(x, sw - w - px(4)),
                                                     min(y, sh - h - px(4))))

    # 对外
    def hide(self):
        for m in ("mini", "full"):
            self._win[m].withdraw()

    def show(self):
        self._apply_mode()

    def ensure_visible(self):
        self._apply_mode()
        self._place()

    @property
    def win(self):
        return self._win[self.mode]

    def set_state(self, s):
        self.state = s
        self._render_all()

    def set_level(self, lvl):
        self.level = lvl
        self._render_all()


class Panel:
    """Fluent 设置面板"""

    W = 316

    def __init__(self, app, anchor_win):
        from fatfish_core import CFG, LANGS, save_cfg
        self.app = app
        cfg = CFG
        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#0a0f1e")
        try:
            self.win.attributes("-transparentcolor", "#0a0f1e")
        except Exception:
            pass
        holder = tk.Canvas(self.win, bg="#0a0f1e", highlightthickness=0)
        holder.pack()
        body = tk.Frame(holder, bg=C_SURFACE)

        head = tk.Frame(body, bg=C_SURFACE)
        head.pack(fill="x", padx=(px(16), px(8)), pady=(px(12), px(4)))
        tk.Label(head, text="语音输入设置", bg=C_SURFACE, fg=C_TEXT,
                 font=fnt(14, "bold")).pack(side="left")
        close = tk.Label(head, text=GLYPH_CLOSE, bg=C_SURFACE, fg=C_TEXT2,
                         font=(ICON_FONT, -px(11)), cursor="hand2", padx=px(6))
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.win.destroy())

        # 一键截图翻译
        self._row(body, GLYPH_CAMERA, "一键截图翻译", "框选屏幕 → 识别 → 翻译",
                  on_click=lambda: (self.win.destroy(), app.shot()), chevron=True)
        self._sep(body)

        # 输出语言
        box = tk.Frame(body, bg=C_SURFACE)
        box.pack(fill="x", padx=px(16), pady=(px(10), px(10)))
        tk.Label(box, text="输出语言", bg=C_SURFACE, fg=C_TEXT, font=fnt(13)).pack(anchor="w")
        chips = tk.Frame(box, bg=C_SURFACE)
        chips.pack(anchor="w", pady=(px(8), 0))
        for lang in LANGS:
            on = (lang == cfg["target_lang"])
            b = tk.Label(chips, text=lang, font=fnt(11, "bold" if on else "normal"),
                         bg=(C_ACCENT if on else C_HOVER), fg=("#FFFFFF" if on else C_TEXT),
                         padx=px(9), pady=px(4), cursor="hand2")
            b.pack(side="left", padx=(0, px(5)))

            def pick(e, l=lang):
                cfg["target_lang"] = l
                save_cfg(cfg)
                self.win.destroy()
                app.open_settings()
            b.bind("<Button-1>", pick)
        self._sep(body)

        # 翻译引擎（自动 / 在线 / 离线）
        engine_row = tk.Frame(body, bg=C_SURFACE, height=px(46))
        engine_row.pack(fill="x")
        engine_row.pack_propagate(False)
        tk.Label(engine_row, text=GLYPH_GLOBE, bg=C_SURFACE, fg=C_TEXT2,
                 font=(ICON_FONT, -px(15))).pack(side="left", padx=(px(16), px(10)))
        ebox = tk.Frame(engine_row, bg=C_SURFACE)
        ebox.pack(side="left", fill="both", expand=True, pady=px(6))
        tk.Label(ebox, text="翻译引擎", bg=C_SURFACE, fg=C_TEXT, font=fnt(12),
                 anchor="w").pack(fill="x")
        self.engine_hint = tk.Label(ebox, text="", bg=C_SURFACE, fg=C_TEXT3, font=fnt(9),
                                    anchor="w")
        self.engine_hint.pack(fill="x")
        eng_btn = tk.Label(engine_row, text="", bg=C_HOVER, fg=C_TEXT, font=fnt(10, "bold"),
                           padx=px(10), pady=px(4), cursor="hand2")
        eng_btn.pack(side="right", padx=(0, px(14)))

        def refresh_engine():
            names = {"auto": "自动", "online": "在线", "offline": "离线"}
            eng_btn.configure(text=names.get(cfg.get("mt_engine", "auto"), "自动"))
            try:
                import fatfish_core as _c
                ok = _c.local_mt_ready()
            except Exception:
                ok = False
            self.engine_hint.configure(text=("本地模型已下载（质量一般，默认仍走在线）" if ok
                                             else "本地模型未下载（走在线）"))

        def cycle_engine(e):
            order = ["auto", "online", "offline"]
            cur = cfg.get("mt_engine", "auto")
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
            cfg["mt_engine"] = nxt
            save_cfg(cfg)
            refresh_engine()
        eng_btn.bind("<Button-1>", cycle_engine)
        refresh_engine()
        self._sep(body)

        # 自动粘贴
        self._row(body, GLYPH_CHECK, "识别后自动粘贴", None,
                  right_widget=lambda parent: Toggle(parent, value=cfg.get("auto_paste", True),
                                                     command=self._set_paste))
        self._sep(body)

        info = tk.Frame(body, bg=C_SURFACE)
        info.pack(fill="x", padx=px(16), pady=(px(8), px(12)))
        tk.Label(info, text="语音识别：本机离线（断网也能听写）", bg=C_SURFACE, fg=C_TEXT2,
                 font=fnt(10), anchor="w").pack(fill="x")
        tk.Label(info, text="翻译：联网 DeepSeek（不需要 DSH）", bg=C_SURFACE, fg=C_TEXT2,
                 font=fnt(10), anchor="w").pack(fill="x", pady=(px(2), 0))
        tk.Label(info, text="%s 开始/停止    %s 截图    %s 换语言"
                           % (cfg["hotkey_voice"], cfg["hotkey_shot"], cfg["hotkey_lang"]),
                 bg=C_SURFACE, fg=C_TEXT3, font=fnt(9), anchor="w").pack(fill="x", pady=(px(8), 0))

        body.update_idletasks()
        bw, bh = body.winfo_reqwidth(), body.winfo_reqheight()
        holder.configure(width=bw, height=bh)
        holder.create_image(0, 0, image=rounded(bw, bh, px(8), C_SURFACE, C_BORDER, px(1)),
                            anchor="nw")
        holder.create_window(0, 0, window=body, anchor="nw")
        self._holder = holder

        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        x = anchor_win.winfo_x() + (anchor_win.winfo_width() - bw) // 2
        y = anchor_win.winfo_y() - bh - px(10)
        if y < px(12):   # 上方放不下就翻到胶囊下面，别压在胶囊上
            y = anchor_win.winfo_y() + anchor_win.winfo_height() + px(10)
        x = max(px(12), min(sw - bw - px(12), x))
        y = max(px(12), min(self.win.winfo_screenheight() - bh - px(12), y))
        self.win.geometry("+%d+%d" % (x, y))
        app.make_noactivate(self.win)

    def _set_paste(self, v):
        from fatfish_core import CFG, save_cfg
        CFG["auto_paste"] = bool(v)
        save_cfg(CFG)

    def _sep(self, parent):
        tk.Frame(parent, bg=C_BORDER, height=max(1, px(1))).pack(fill="x")

    def _row(self, parent, glyph, title, sub=None, on_click=None, chevron=False,
             right_widget=None):
        row = tk.Frame(parent, bg=C_SURFACE, height=px(46))
        row.pack(fill="x")
        row.pack_propagate(False)
        icon = tk.Label(row, text=glyph, bg=C_SURFACE, fg=C_TEXT2, font=(ICON_FONT, -px(15)))
        icon.pack(side="left", padx=(px(16), px(10)))
        box = tk.Frame(row, bg=C_SURFACE)
        box.pack(side="left", fill="both", expand=True, pady=px(6))
        tk.Label(box, text=title, bg=C_SURFACE, fg=C_TEXT, font=fnt(12), anchor="w").pack(fill="x")
        if sub:
            tk.Label(box, text=sub, bg=C_SURFACE, fg=C_TEXT3, font=fnt(9), anchor="w").pack(fill="x")
        if right_widget:
            right_widget(row).pack(side="right", padx=(0, px(14)))
        elif chevron:
            tk.Label(row, text=GLYPH_CHEVRON, bg=C_SURFACE, fg=C_TEXT3,
                     font=(ICON_FONT, -px(11))).pack(side="right", padx=(0, px(14)))
        if on_click:
            for w in (row, box, icon):
                w.bind("<Button-1>", lambda e: on_click())
                w.bind("<Enter>", lambda e: [x.configure(bg=C_HOVER) for x in (row, box, icon)])
                w.bind("<Leave>", lambda e: [x.configure(bg=C_SURFACE) for x in (row, box, icon)])
        return row


class Card:
    def __init__(self, app, title, body=None, image_path=None, anchor_win=None, copy_text=None):
        from PIL import Image, ImageTk
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#0a0f1e")
        try:
            self.win.attributes("-transparentcolor", "#0a0f1e")
        except Exception:
            pass
        holder = tk.Canvas(self.win, bg="#0a0f1e", highlightthickness=0)
        holder.pack()
        inner = tk.Frame(holder, bg=C_SURFACE)

        head = tk.Frame(inner, bg=C_SURFACE)
        head.pack(fill="x", padx=(px(14), px(8)), pady=(px(10), px(2)))
        tk.Label(head, text=title, bg=C_SURFACE, fg=C_TEXT, font=fnt(12, "bold")).pack(side="left")
        close = tk.Label(head, text=GLYPH_CLOSE, bg=C_SURFACE, fg=C_TEXT2,
                         font=(ICON_FONT, -px(11)), cursor="hand2", padx=px(6))
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.close())

        self.meter = None
        if body is None:
            self.meter = tk.Canvas(inner, height=px(28), bg=C_SURFACE, highlightthickness=0)
            self.meter.pack(fill="x", padx=px(14), pady=(px(2), px(0)))
            self.bars = [self.meter.create_rectangle(0, 0, 0, 0, fill=C_ACCENT, outline="")
                         for _ in range(26)]
            self.status = tk.Label(inner, text="正在聆听…", bg=C_SURFACE, fg=C_TEXT2, font=fnt(10))
            self.status.pack(pady=(px(2), px(12)))
        else:
            if image_path and os.path.exists(image_path):
                im = Image.open(image_path)
                im.thumbnail((px(520), px(260)))
                self._photo = ImageTk.PhotoImage(im)
                tk.Label(inner, image=self._photo, bg=C_SURFACE).pack(padx=px(14), pady=px(6))
            box = tk.Frame(inner, bg=C_SURFACE)
            box.pack(padx=px(14), pady=(0, px(8)))
            self.txt = tk.Text(box, height=8, width=58, bg="#FAFAFA", fg=C_TEXT, relief="flat",
                               font=fnt(11), wrap="word", padx=px(8), pady=px(8),
                               highlightthickness=px(1), highlightbackground=C_BORDER)
            self.txt.pack()
            self.txt.insert("1.0", body)
            btns = tk.Frame(inner, bg=C_SURFACE)
            btns.pack(fill="x", padx=px(14), pady=(0, px(10)))
            b1 = tk.Label(btns, text="复制译文", bg=C_ACCENT, fg="#FFFFFF", font=fnt(10, "bold"),
                          padx=px(12), pady=px(4), cursor="hand2")
            b1.pack(side="left")
            _copy = copy_text if copy_text else body
            b1.bind("<Button-1>", lambda e: __import__("fatfish_core").clip_set(_copy))
            if image_path:
                b2 = tk.Label(btns, text="打开译文图片", bg=C_HOVER, fg=C_TEXT, font=fnt(10),
                              padx=px(12), pady=px(4), cursor="hand2")
                b2.pack(side="left", padx=px(8))
                b2.bind("<Button-1>", lambda e: os.startfile(image_path))

        inner.update_idletasks()
        iw, ih = inner.winfo_reqwidth(), inner.winfo_reqheight()
        holder.configure(width=iw, height=ih)
        holder.create_image(0, 0, image=rounded(iw, ih, px(8), C_SURFACE, C_BORDER, px(1)),
                            anchor="nw")
        holder.create_window(0, 0, window=inner, anchor="nw")

        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        if anchor_win is not None:
            x = anchor_win.winfo_x() + (anchor_win.winfo_width() - iw) // 2
            y = anchor_win.winfo_y() - ih - px(10)
        else:
            x, y = (sw - iw) // 2, self.win.winfo_screenheight() // 2
        x = max(px(12), min(sw - iw - px(12), x))
        y = max(px(12), y)
        self.win.geometry("+%d+%d" % (x, y))
        app.make_noactivate(self.win)

    def update_level(self, lvl):
        if not self.meter:
            return
        h = px(28)
        for i, item in enumerate(self.bars):
            amp = min(1.0, lvl * 18) * (0.35 + 0.65 * abs((i % 7) - 3) / 3)
            bh = max(px(2), int(h * amp))
            x = px(4) + i * px(10)
            self.meter.coords(item, x, (h - bh) // 2, x + px(5), (h + bh) // 2)

    def set_status(self, s):
        if self.meter:
            self.status.configure(text=s)

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass
