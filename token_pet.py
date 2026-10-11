#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kagami酱 —— 常驻桌面右下角的 Q 版小跟班（原型：幸运星·柊镜），
实时记录 ZCode 的 token 用量。

数据源:  ~/.zcode/cli/rollout/model-io-sess_*.jsonl （每次模型调用一行）
用法:    python token_pet.py            启动桌面宠物
         python token_pet.py --cli      终端里打印统计报告
         python token_pet.py --debug    打印窗口坐标（调试用）
仅用 Python 标准库。配置与累计状态存于注册表 HKCU\\Software\\TokenPet。
"""
import argparse
import glob
import json
import math
import os
import queue
import secrets
import sys
import threading
import time
import tkinter as tk
import winreg
from datetime import datetime, timedelta
from pathlib import Path
from pathlib import Path

APP_NAME = "Kagami酱"
ROLL_DIR = Path.home() / ".zcode" / "cli" / "rollout"
REG_KEY = r"Software\TokenPet"

MAGIC = "#010101"          # 透明键色，不要在画面里使用
OUTLINE = "#43306B"
W, H = 200, 284            # 画布尺寸：顶部留给气泡

DEFAULT_CFG = {
    "pos": None,            # [x, y]
    "topmost": True,
    "show_today": True,
    "chirp": True,
    "rates": None,          # {"input": 每百万, "output": ..., "cache": ...}
    "currency": "¥",
}

# 内置单价表（美元/每百万 tokens，仅收录有公开定价把握的模型；
# 其余模型不显示成本，用户可在"成本单价设置"里手动覆盖全部模型）
BUILTIN_PRICES = {
    "deepseek-chat":     {"input": 0.28, "output": 0.42, "cache": 0.028},
    "deepseek-reasoner": {"input": 0.28, "output": 0.42, "cache": 0.028},
    "glm-4.6":           {"input": 0.6,  "output": 2.2,  "cache": 0.11},
    "glm-4.5":           {"input": 0.6,  "output": 2.2,  "cache": 0.11},
    "glm-4.5-air":       {"input": 0.2,  "output": 0.94, "cache": 0.02},
    "glm-4.5-flash":     {"input": 0.0,  "output": 0.0,  "cache": 0.0},
}


def lookup_price(model):
    key = str(model).lower()
    if key in BUILTIN_PRICES:
        return BUILTIN_PRICES[key]
    for prefix, p in BUILTIN_PRICES.items():
        if key.startswith(prefix):
            return p
    return None


# ---------------------------------------------------------------- 工具

def rnd(a, b):
    """装饰性动画用的随机小数。"""
    return a + (b - a) * (secrets.randbelow(100000) / 100000.0)


def compact(n):
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1000_000:.2f}M"


def day_str(ts):
    return datetime.fromtimestamp(ts).strftime("%m-%d")


def predict_text(rate15):
    """按近 15 分钟速率外推的傲娇预警；速率太低不说话。"""
    if rate15 < 2000:
        return None
    return (f"每分钟 {compact(rate15)}…两小时 {compact(rate15 * 120)}，"
            f"节制点啊笨蛋主人！")


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _reg_get(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            value, _ = winreg.QueryValueEx(k, name)
            return json.loads(value)
    except OSError:
        return None


def _reg_set(name, value):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ,
                          json.dumps(value, ensure_ascii=False))


def load_config():
    return {**DEFAULT_CFG, **(_reg_get("config") or {})}


def save_config(cfg):
    _reg_set("config", cfg)


def load_state():
    return _reg_get("state") or {}


def save_state(state):
    _reg_set("state", state)


# -- 开机自启（注册表 Run 键，Windows 原生机制） --------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "KagamiPet"


def _pythonw():
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        return exe
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else None


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
        return True
    except OSError:
        return False


def autostart_set(enable):
    if enable:
        pw = _pythonw()
        if not pw:
            raise OSError("pythonw.exe not found")
        cmd = f'"{pw}" "{Path(__file__).resolve()}"'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, RUN_VALUE)
        except FileNotFoundError:
            pass


NOTIFY_KEY = r"Control Panel\NotifyIconSettings"


def ensure_tray_visible():
    """Win11 默认把新托盘图标锁在溢出区；把自己的项设为允许提升并显示。"""
    try:
        my_exe = os.path.normcase(sys.executable)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_KEY, 0,
                            winreg.KEY_READ) as base:
            target = None
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, i)
                    i += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(base, sub) as k:
                        exe = winreg.QueryValueEx(k, "ExecutablePath")[0]
                except OSError:
                    continue
                if os.path.normcase(exe) == my_exe:
                    target = sub
                    break
        if target:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                NOTIFY_KEY + "\\" + target, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "IsPromotable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "IsShown", 0, winreg.REG_DWORD, 1)
    except Exception:
        pass


# ---------------------------------------------------------------- 统计引擎

class Engine:
    """增量扫描 model-io JSONL，聚合 token 用量。

    lifetime（累计总量）通过 per-file contrib 记账，即使文件被清理也不会丢；
    日/模型/会话视图来自磁盘上现存文件的记录。
    """

    def __init__(self, rollout_dir, persistent=True):
        self.dir = Path(rollout_dir)
        self.persistent = persistent   # 测试模式不读写注册表，防污染真实账本
        self.recs = []          # (ts, sid, model, in_tok, out_tok, c_read, c_write)
        state = load_state() if persistent else {}
        self.lifetime = state.get("lifetime", {})
        self.files = state.get("files", {})   # path -> {size, offset, contrib, tail}
        self.seen_rids = set()                # requestId 去重（防重试双计）
        self.dup_skipped = 0
        self.last_scan = 0
        self.rescan_full()

    # -- 记账 ------------------------------------------------------------
    def _zero(self):
        return {"in": 0, "out": 0, "cread": 0, "cwrite": 0, "req": 0}

    def _add(self, bucket, c, k=1):
        for key in ("in", "out", "cread", "cwrite", "req"):
            bucket[key] = bucket.get(key, 0) + c[key] * k

    def rescan_full(self):
        """启动时全量重放磁盘上的文件（lifetime 先扣旧账再重新记账，不重复）。"""
        for path in list(self.files):
            self._add(self.lifetime, self.files[path]["contrib"], k=-1)
        self.files = {}
        self.recs = []
        self.seen_rids = set()
        self.dup_skipped = 0
        self._scan(force=True)

    def _scan(self, force=False):
        try:
            paths = {str(p) for p in self.dir.glob("*.jsonl")}
        except OSError:
            paths = set()
        # 文件被删除：从累计账本里扣掉它贡献过的部分
        for path in list(self.files):
            if path not in paths:
                self._add(self.lifetime, self.files[path]["contrib"], k=-1)
                del self.files[path]
        changed = force
        for path in paths:
            try:
                size = Path(path).stat().st_size
            except OSError:
                continue
            f = self.files.get(path)
            if f is None:
                f = self.files[path] = {"size": 0, "offset": 0,
                                        "contrib": self._zero(), "tail": ""}
            if f["size"] and size < f["size"]:      # 被截断/替换：重置该文件
                self._add(self.lifetime, f["contrib"], k=-1)
                f.update(offset=0, size=0, contrib=self._zero(), tail="")
            if not force and size == f["size"] and f["offset"] == size:
                continue
            changed = True
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            text = f["tail"] + data[f["offset"]:].decode("utf-8",
                                                         errors="replace")
            if text.endswith("\n"):
                f["tail"] = ""
            else:
                cut = text.rfind("\n") + 1
                f["tail"] = text[cut:]
                text = text[:cut]
            f["offset"] = len(data) - len(f["tail"].encode("utf-8"))
            f["size"] = size
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                usage = (obj.get("response") or {}).get("usage")
                if not usage:
                    continue
                rid = obj.get("requestId")
                if rid:                      # 同一请求只计一次（ccusage 式去重）
                    if rid in self.seen_rids:
                        self.dup_skipped += 1
                        continue
                    self.seen_rids.add(rid)
                ts = parse_iso(obj.get("startedAt") or "") or time.time()
                m = obj.get("model")
                model = m.get("modelId") if isinstance(m, dict) else (m or "?")
                rec = (ts, obj.get("sessionId") or "?", model,
                       usage.get("inputTokens", 0), usage.get("outputTokens", 0),
                       usage.get("cacheReadTokens", 0),
                       usage.get("cacheWriteTokens", 0))
                self.recs.append(rec)
                c = f["contrib"]
                c["in"] += rec[3]; c["out"] += rec[4]
                c["cread"] += rec[5]; c["cwrite"] += rec[6]; c["req"] += 1
                self._add(self.lifetime, {"in": rec[3], "out": rec[4],
                                          "cread": rec[5], "cwrite": rec[6],
                                          "req": 1})
        if changed and self.persistent:
            self.last_scan = time.time()
            save_state({"lifetime": self.lifetime, "files": self.files})
        return changed

    # -- 视图 ------------------------------------------------------------
    def snapshot(self):
        now = time.time()
        today0 = datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        week0 = today0 - 6 * 86400
        views = {"today": self._zero(), "week": self._zero(),
                 "days": {}, "models": {}, "sessions": {},
                 "today_models": {}}
        for ts, sid, model, it, ot, cr, cw in self.recs:
            c = {"in": it, "out": ot, "cread": cr, "cwrite": cw, "req": 1}
            if ts >= today0:
                self._add(views["today"], c)
                views["today_models"].setdefault(model, self._zero())
                self._add(views["today_models"][model], c)
            if ts >= week0:
                self._add(views["week"], c)
                views["days"].setdefault(day_str(ts), self._zero())
                self._add(views["days"][day_str(ts)], c)
            views["models"].setdefault(model, self._zero())
            self._add(views["models"][model], c)
            views["sessions"].setdefault(sid, {"zero": self._zero(), "last": 0})
            self._add(views["sessions"][sid]["zero"], c)
            views["sessions"][sid]["last"] = max(views["sessions"][sid]["last"],
                                                 ts)
        rate = sum(r[3] + r[4] for r in self.recs if r[0] >= now - 60)
        rate15 = sum(r[3] + r[4] for r in self.recs if r[0] >= now - 900) / 15
        last_act = max((r[0] for r in self.recs), default=0)
        cur_sid = max(views["sessions"].items(), key=lambda kv: kv[1]["last"],
                      default=(None, None))[0]
        return {"now": now, "today0": today0, **views,
                "rate60": rate, "rate15": rate15, "last_act": last_act,
                "cur_sid": cur_sid, "lifetime": self.lifetime}

    def cost_models(self, models_view):
        """按模型单价估算成本（美元）。用户配置了统一单价则优先；
        有任一模型不在价格表内且无用户配置 → 返回 None（宁缺勿错）。"""
        cfg = load_config()
        rates = cfg.get("rates")
        if rates:
            total = 0.0
            for b in models_view.values():
                total += ((b["in"] - b["cread"]) / 1e6
                          * rates.get("input", 0)
                          + b["out"] / 1e6 * rates.get("output", 0)
                          + b["cread"] / 1e6 * rates.get("cache", 0))
            return total, cfg.get("currency", "¥")
        total = 0.0
        for model, b in models_view.items():
            p = lookup_price(model)
            if p is None:
                return None, "$"
            total += ((b["in"] - b["cread"]) / 1e6 * p["input"]
                      + b["out"] / 1e6 * p["output"]
                      + b["cread"] / 1e6 * p["cache"])
        return total, "$"

    def audit(self):
        """独立重扫磁盘，与引擎内存账核对（数据自检）。"""
        files = sorted(glob.glob(os.path.join(self.dir, "*.jsonl")))
        rows = parse_fail = no_usage = 0
        for path in files:
            try:
                raw = Path(path).read_bytes()
            except OSError:
                continue
            for line in raw.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                rows += 1
                try:
                    obj = json.loads(line)
                except Exception:
                    parse_fail += 1
                    continue
                if not (obj.get("response") or {}).get("usage"):
                    no_usage += 1
        expected = rows - parse_fail - no_usage
        return {"files": len(files), "rows": rows,
                "parse_fail": parse_fail, "no_usage": no_usage,
                "expected": expected, "engine_records": len(self.recs),
                "dedup_skipped": self.dup_skipped,
                "lifetime_req": self.lifetime.get("req", 0)}


# ---------------------------------------------------------------- 宠物窗口

PALETTE = {
    "hair": "#A47BEA", "hair_dark": "#7A55C9", "hair_deep": "#6A48B5",
    "skin": "#FFE9D4",
    "eye": "#4A7BE0", "ribbon": "#F2578F", "ribbon_dark": "#D63E75",
    "top": "#FFFFFF", "collar": "#A47BEA", "scarf": "#F2578F",
    "skirt": "#7C57C9", "shoe": "#5B3E9E", "blush": "#FFB3C2",
    "mouth": "#A63A50", "bubble_bg": "#FBF7FF",
}

HEAD_LINES = [
    "哈？！干、干嘛突然摸我！",
    "都、都说了别摸头！…哼",
    "……就、就原谅你这一次",
    "喂喂，别摸了，认真工作啦！",
    "唔…也、也不是不可以再摸一下",
]


class PetApp:
    def __init__(self, root, engine, cfg):
        self.root = root
        self.engine = engine
        self.cfg = cfg
        self.snap = engine.snapshot()
        self.state = "idle"          # idle / working / sweat / sleep / party
        self.bubble_until = 0
        self.party_until = 0
        self.last_chirp = 0
        self._last_dy = 0
        self._last_dx = 0
        self.drag = None
        self.moved = False
        self.tooltip = None
        self.tooltip_after = None
        self.panel = None
        self.tick_n = 0

        st = load_state()
        today_key = datetime.now().strftime("%Y-%m-%d")
        if st.get("milestone_day") != today_key:
            self.milestone_done = set()
        else:
            self.milestone_done = set(st.get("milestones", []))
        # 启动时把已有的量级预标为完成，避免用历史数据倒放庆祝
        t0 = self.snap["today"]["in"] + self.snap["today"]["out"]
        self.milestone_done |= set(range(1, int(t0 // 100_000) + 1))

        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        root.overrideredirect(True)
        root.configure(bg=MAGIC)
        try:
            root.attributes("-transparentcolor", MAGIC)
        except Exception:
            pass
        self._apply_topmost()
        self.canvas = tk.Canvas(root, width=W, height=H, bg=MAGIC,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self._spawn_position()
        self._draw_pet()
        self._bind()
        self._setup_tray()
        self.root.after(80, self._tick)

    # -- 位置 ------------------------------------------------------------
    def _spawn_position(self):
        pos = self.cfg.get("pos")
        if pos:
            x, y = pos
        else:
            try:
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
                x, y = sw - W - 40, sh - H - 90
            except Exception:
                x, y = 100, 100
        self.root.geometry(f"{W}x{H}+{x}+{y}")

    def _apply_topmost(self):
        try:
            self.root.attributes("-topmost", bool(self.cfg.get("topmost", True)))
        except Exception:
            pass

    # -- 托盘 ------------------------------------------------------------
    def _tray_image(self):
        try:
            from PIL import Image as PILImage
            base = Path(__file__).parent
            path = self.cfg.get("pet_image") or str(base / "pet_sprite.png")
            img = PILImage.open(path).convert("RGBA")
            side = max(img.size)
            sq = PILImage.new("RGBA", (side, side), (0, 0, 0, 0))
            sq.paste(img, ((side - img.width) // 2,
                           (side - img.height) // 2))
            return sq.resize((64, 64), PILImage.LANCZOS)
        except Exception:
            try:
                from PIL import Image as PILImage
                return PILImage.new("RGBA", (64, 64), (138, 99, 217, 255))
            except Exception:
                return None

    def _setup_tray(self):
        """系统托盘：左键单击 = 显示/隐藏，右键 = 菜单。失败则静默降级。"""
        self.tray_q = queue.Queue()
        self.icon = None
        try:
            import pystray
        except ImportError:
            return
        img = self._tray_image()
        if img is None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示 / 隐藏",
                             lambda *a: self.tray_q.put("toggle"), default=True),
            pystray.MenuItem("统计面板", lambda *a: self.tray_q.put("panel")),
            pystray.MenuItem("立即刷新", lambda *a: self.tray_q.put("refresh")),
            pystray.MenuItem("开机自启", lambda *a: self.tray_q.put("autostart"),
                             checked=lambda *a: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda *a: self.tray_q.put("quit")),
        )
        self.icon = pystray.Icon("TokenPet", img,
                                 f"{APP_NAME} · Token 记录中", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()
        # 图标注册进溢出区后，再提升到任务栏（立即写会和注册竞态）
        self.root.after(3000, ensure_tray_visible)
        self.root.after(150, self._tray_poll)
        self._tray_title_at = 0

    def _tray_poll(self):
        try:
            while True:
                cmd = self.tray_q.get_nowait()
                if cmd == "toggle":
                    self._toggle_visible()
                elif cmd == "panel":
                    if self.root.state() == "withdrawn":
                        self.root.deiconify()
                    self._toggle_panel()
                elif cmd == "refresh":
                    self._force_scan()
                elif cmd == "autostart":
                    self._toggle_autostart()
                elif cmd == "quit":
                    self._quit()
                    return
        except queue.Empty:
            pass
        self.root.after(120, self._tray_poll)

    def _toggle_visible(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.lift()
        else:
            self.root.withdraw()

    def _set_tray_title(self, text):
        if self.icon is not None:
            try:
                self.icon.title = text
            except Exception:
                pass

    # -- 绘制 ------------------------------------------------------------
    def _load_sprite(self):
        base = Path(__file__).parent
        path = self.cfg.get("pet_image") or str(base / "pet_sprite.png")
        try:
            img = tk.PhotoImage(file=str(path), master=self.root)
            if img.height() > 240:
                factor = max(1, round(img.height() / 210))
                img = img.subsample(factor, factor)
            return img
        except Exception:
            return None

    def _draw_pet(self):
        c = self.canvas
        self.pet_items = []
        sprite = self._load_sprite()
        if sprite is not None:
            self.sprite = sprite        # 必须保活，PhotoImage 被 GC 图片会消失
            self.sprite_item = c.create_image(100, 152, image=self.sprite,
                                              anchor="center")
            self.pet_items.append(self.sprite_item)
        else:
            self.sprite_item = None
            # 没有精灵图时的占位圆脸
            face = c.create_oval(58, 78, 142, 172, fill="#FFE9D4",
                                 outline=OUTLINE, width=2)
            eye1 = c.create_oval(76, 112, 88, 134, fill="#4A7BE0",
                                 outline="")
            eye2 = c.create_oval(112, 112, 124, 134, fill="#4A7BE0",
                                 outline="")
            mouth = c.create_arc(88, 136, 112, 154, start=20, extent=140,
                                 style="arc", outline=OUTLINE, width=2)
            hint = c.create_text(100, 240, text="缺少 pet_sprite.png",
                                 fill="#8A63D9",
                                 font=("Microsoft YaHei UI", 8))
            self.pet_items += [face, eye1, eye2, mouth, hint]

        # 汗滴 / 星星 / Zzz（默认隐藏）
        self.sweat = c.create_polygon([158, 92, 163, 101, 158, 106,
                                       153, 101], fill="#7EC8F0",
                                      outline="#3E93C8", width=1)
        self._hide(self.sweat)
        self.sparks = [c.create_polygon(self._star(x, y, s), fill="#FFD966",
                                        outline="", smooth=True)
                       for x, y, s in ((28, 52, 8), (172, 48, 9),
                                       (100, 12, 6))]
        for s_ in self.sparks:
            self._hide(s_)
        self.zzz = [c.create_text(168, 92 + i * 13, text="z",
                                  fill="#9B72E8",
                                  font=("Segoe UI", 10 + i * 3, "bold"))
                    for i in range(3)]
        for z in self.zzz:
            self._hide(z)

        # 摸头小心心（默认隐藏，单击小人时飘起）
        self.hearts = [c.create_polygon(self._heart(100, 60, 7),
                                        fill="#F98BB0", outline="",
                                        smooth=True) for _ in range(3)]
        for h in self.hearts:
            self._hide(h)
        self.hearts_until = 0
        self.heart_start = 0
        self.heart_pos = []

        # 今日数值小标签
        self.today_label = c.create_text(
            100, 262, text="", fill="#43306B",
            font=("Microsoft YaHei UI", 9, "bold"))

        # 气泡
        self.bubble = c.create_polygon(
            self._round_rect(8, 4, 192, 58, 14), fill=PALETTE["bubble_bg"],
            outline="#C9B6EE", width=2, smooth=True)
        self.bubble_tail = c.create_polygon(
            [88, 56, 108, 56, 98, 72], fill=PALETTE["bubble_bg"],
            outline="", smooth=True)
        self.bubble_text = c.create_text(
            100, 31, text="", fill="#43306B", width=176,
            font=("Microsoft YaHei UI", 10), justify="center")
        for item in (self.bubble, self.bubble_tail, self.bubble_text):
            self._hide(item)

    @staticmethod
    def _heart(cx, cy, s):
        return [cx, cy - 0.5 * s,
                cx - s, cy - 1.4 * s,
                cx - 1.6 * s, cy - 0.2 * s,
                cx, cy + 1.2 * s,
                cx + 1.6 * s, cy - 0.2 * s,
                cx + s, cy - 1.4 * s]

    @staticmethod
    def _star(cx, cy, r):
        pts = []
        for i in range(8):
            ang = math.pi / 4 * i - math.pi / 2
            rad = r if i % 2 == 0 else r * 0.4
            pts += [cx + rad * math.cos(ang), cy + rad * math.sin(ang)]
        return pts

    @staticmethod
    def _round_rect(x1, y1, x2, y2, r):
        return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r,
                x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1,
                y1 + r, x1, y1]

    def _hide(self, item):
        self.canvas.itemconfig(item, state="hidden")

    def _show(self, item):
        self.canvas.itemconfig(item, state="normal")

    # -- 交互 ------------------------------------------------------------
    def _bind(self):
        cv = self.canvas
        cv.bind("<ButtonPress-1>", self._press)
        cv.bind("<B1-Motion>", self._motion)
        cv.bind("<ButtonRelease-1>", self._release)
        cv.bind("<Button-3>", self._menu)
        cv.bind("<Enter>", self._hover_enter)
        cv.bind("<Leave>", self._hover_leave)

    def _press(self, e):
        self.drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self.moved = False
        if self.tooltip_after:
            self.root.after_cancel(self.tooltip_after)
            self.tooltip_after = None

    def _motion(self, e):
        if not self.drag:
            return
        dx, dy = e.x_root - self.drag[0], e.y_root - self.drag[1]
        if abs(dx) + abs(dy) > 4:
            self.moved = True
            self.root.geometry(f"+{self.drag[2] + dx}+{self.drag[3] + dy}")

    def _release(self, e):
        if self.drag and not self.moved and e.num == 1:
            now = time.time()
            if now - getattr(self, "_last_click", 0) < 0.45:
                self._last_click = 0
                self._toggle_panel()            # 双击：统计面板
            else:
                self._last_click = now
                self._pet_head()                # 单击：摸头
        if self.drag and self.moved:
            self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            save_config(self.cfg)
        self.drag = None

    def _hide_bubble(self):
        for item in (self.bubble, self.bubble_tail, self.bubble_text):
            self._hide(item)
        self.bubble_until = 0

    def _pet_head(self):
        """摸头：收起当前气泡 → 心独占 1 秒 → 傲娇台词登场。"""
        self._hide_bubble()
        now = time.time()
        self.hearts_until = now + 1.3
        self.heart_start = now
        self.heart_pos = [(74 + secrets.randbelow(52), 58)
                          for _ in range(3)]
        self.root.after(1000, lambda: self._say(
            secrets.choice(HEAD_LINES), 3.5))

    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0, font=("Microsoft YaHei UI", 9))
        m.add_command(label="📊 统计面板", command=self._toggle_panel)
        m.add_command(label="🔄 立即刷新", command=self._force_scan)
        m.add_separator()
        m.add_checkbutton(label="窗口置顶", command=self._toggle_topmost)
        m.add_checkbutton(label="显示今日数值", command=self._toggle_today)
        m.add_checkbutton(label="闲聊气泡", command=self._toggle_chirp)
        m.add_separator()
        m.add_checkbutton(label="🚀 开机自启",
                          variable=tk.BooleanVar(
                              value=autostart_enabled()),
                          command=self._toggle_autostart)
        m.add_command(label="🙈 隐藏小人（托盘唤回）", command=self._toggle_visible)
        m.add_command(label="💰 成本单价设置…", command=self._price_dialog)
        m.add_command(label="📍 重置位置", command=self._reset_pos)
        m.add_command(label="❌ 退出", command=self._quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _toggle_topmost(self):
        self.cfg["topmost"] = not self.cfg.get("topmost", True)
        self._apply_topmost()
        save_config(self.cfg)

    def _toggle_today(self):
        self.cfg["show_today"] = not self.cfg.get("show_today", True)
        save_config(self.cfg)

    def _toggle_chirp(self):
        self.cfg["chirp"] = not self.cfg.get("chirp", True)
        save_config(self.cfg)

    def _reset_pos(self):
        self.cfg["pos"] = None
        save_config(self.cfg)
        self._spawn_position()

    def _toggle_autostart(self):
        try:
            autostart_set(not autostart_enabled())
        except OSError:
            self._say("找不到 pythonw.exe…自启设置失败了")
            return
        self._say("开机自启已" +
                  ("开启！下次开机会等你哦" if autostart_enabled()
                   else "关闭"))

    def _force_scan(self):
        self.engine._scan(force=True)
        self.snap = self.engine.snapshot()

    def _quit(self):
        try:
            if self.engine.persistent:
                save_state({"lifetime": self.engine.lifetime,
                            "files": self.engine.files})
        finally:
            try:
                if self.icon is not None:
                    self.icon.stop()
            except Exception:
                pass
            self.root.destroy()

    # -- 气泡 / 提示 ------------------------------------------------------
    def _say(self, text, seconds=5):
        c = self.canvas
        c.itemconfig(self.bubble_text, text=text)
        # 气泡按文字实际高度自适应重绘，避免溢出
        bb = c.bbox(self.bubble_text)
        text_h = (bb[3] - bb[1]) if bb else 40
        top = 4
        bottom = max(52, text_h + 26)
        c.coords(self.bubble, self._round_rect(8, top, 192, bottom, 14))
        c.coords(self.bubble_tail, [88, bottom - 2, 108, bottom - 2,
                                    98, bottom + 16])
        c.coords(self.bubble_text, 100, (top + bottom) / 2)
        for item in (self.bubble, self.bubble_tail, self.bubble_text):
            self._show(item)
        self.bubble_until = time.time() + seconds

    def _hover_enter(self, _e):
        if self.tooltip_after or self.drag:
            return
        self.tooltip_after = self.root.after(350, self._show_tooltip)

    def _hover_leave(self, _e):
        if self.tooltip_after:
            self.root.after_cancel(self.tooltip_after)
            self.tooltip_after = None
        self._hide_tooltip()

    def _show_tooltip(self):
        self.tooltip_after = None
        self.snap = self.engine.snapshot()
        t = self.snap["today"]
        cost, sym = self.engine.cost_models(self.snap["today_models"])
        lines = [
            f"🍪 今日  输入 {compact(t['in'] - t['cread'])} + 缓存 {compact(t['cread'])}"
            f"  输出 {compact(t['out'])}",
            f"📦 合计 {compact(t['in'] + t['out'])} tokens · {t['req']} 次请求",
        ]
        if cost is not None:
            lines.append(f"💰 约 {sym}{cost:.2f}")
        week = self.snap["week"]
        lt = self.snap["lifetime"]
        lines.append(f"🗓 近7天 {compact(week['in'] + week['out'])} · "
                     f"累计 {compact(lt['in'] + lt['out'])}")
        lines.append("（左键点我看详细报告 · 右键菜单）")
        if self.tooltip:
            self.tooltip.destroy()
        self.tooltip = tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(tw, text="\n".join(lines), justify="left", bg="#2B2140",
                 fg="#F4EFFF", font=("Microsoft YaHei UI", 9),
                 padx=10, pady=8).pack()
        x = self.root.winfo_x() + W + 8
        y = self.root.winfo_y() + 40
        tw.update_idletasks()
        if x + tw.winfo_width() > self.root.winfo_screenwidth() - 10:
            x = self.root.winfo_x() - tw.winfo_width() - 8
        tw.geometry(f"+{x}+{y}")

    def _hide_tooltip(self):
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None

    # -- 统计面板 ---------------------------------------------------------
    def _toggle_panel(self):
        if self.panel and self.panel.winfo_exists():
            self.panel.destroy()
            self.panel = None
            return
        self.panel = pw = tk.Toplevel(self.root)
        pw.title(f"{APP_NAME} · Token 报告")
        pw.configure(bg="#221A38")
        pw.attributes("-topmost", True)
        pw.resizable(False, False)
        self._fill_panel(pw)

    def _fill_panel(self, pw):
        for w_ in pw.winfo_children():
            w_.destroy()
        self.snap = self.engine.snapshot()
        s = self.snap

        def row(text, fg="#EDE6FF", bold=False, size=9, pad=2):
            tk.Label(pw, text=text, bg="#221A38", fg=fg,
                     font=("Microsoft YaHei UI", size,
                           "bold" if bold else "normal"), anchor="w"
                     ).pack(fill="x", padx=14, pady=pad)

        tk.Label(pw, text=f"📊 {APP_NAME} · Token 报告", bg="#221A38",
                 fg="#C9B6EE", font=("Microsoft YaHei UI", 12, "bold")
                 ).pack(pady=(10, 4))
        t = s["today"]
        lt = s["lifetime"]
        row(f"今天  合计 {compact(t['in'] + t['out'])} tokens"
            f"（{t['req']} 次请求）", bold=True, size=11)
        row(f"   纯输入 {compact(t['in'] - t['cread'])} · "
            f"缓存读 {compact(t['cread'])} · 输出 {compact(t['out'])}",
            fg="#B9A8E6")
        cost, sym = self.engine.cost_models(s["today_models"])
        if cost is not None:
            row(f"   估算成本 ≈ {sym}{cost:.2f}（内置单价表或自定义费率）",
                fg="#FFD966")

        # 近 7 天柱状图
        chart = tk.Canvas(pw, width=330, height=110, bg="#2B2140",
                          highlightthickness=0)
        chart.pack(padx=14, pady=6)
        days = []
        for i in range(6, -1, -1):
            d0 = s["today0"] - i * 86400
            label = datetime.fromtimestamp(d0).strftime("%m-%d")
            b = s["days"].get(label) or self.engine._zero()
            days.append((label, b["in"] + b["out"], d0 == s["today0"]))
        mx = max((v for _, v, _ in days), default=1) or 1
        for i, (label, v, is_today) in enumerate(days):
            bx = 18 + i * 44
            bh = max(3, 62 * v / mx)
            color = "#F2578F" if is_today else "#8A63D9"
            chart.create_rectangle(bx, 84 - bh, bx + 26, 84, fill=color,
                                   outline="")
            chart.create_text(bx + 13, 92, text=label, fill="#B9A8E6",
                              font=("Microsoft YaHei UI", 7))
            chart.create_text(bx + 13, 84 - bh - 8, text=compact(v),
                              fill="#EDE6FF", font=("Microsoft YaHei UI", 7))

        row("按模型", fg="#C9B6EE", bold=True)
        for model, b in sorted(s["models"].items(),
                               key=lambda kv: -(kv[1]["in"] + kv[1]["out"])):
            row(f"   {model}  {compact(b['in'] + b['out'])}"
                f"（{b['req']} 次）", size=8)
        row("最近的会话", fg="#C9B6EE", bold=True)
        for sid, info in sorted(s["sessions"].items(),
                                key=lambda kv: -kv[1]["last"])[:4]:
            b = info["zero"]
            when = datetime.fromtimestamp(info["last"]).strftime("%m-%d %H:%M")
            tag = " ← 当前" if sid == s["cur_sid"] else ""
            row(f"   {when}  {compact(b['in'] + b['out'])}{tag}", size=8)
        row(f"🏆 累计 {compact(lt['in'] + lt['out'])} tokens · "
            f"{lt['req']} 次请求（记录自 {APP_NAME} 启用起）",
            fg="#FFD966", bold=True, pad=6)

    # -- 价格设置 ---------------------------------------------------------
    def _price_dialog(self):
        pd = tk.Toplevel(self.root)
        pd.title("成本单价设置")
        pd.configure(bg="#221A38")
        pd.attributes("-topmost", True)
        rates = self.cfg.get("rates") or {}
        tk.Label(pd, text="每百万 tokens 单价（留空则不显示成本）",
                 bg="#221A38", fg="#C9B6EE",
                 font=("Microsoft YaHei UI", 9)).pack(padx=14, pady=6)
        vars_ = {}
        for key, label in (("input", "输入"), ("output", "输出"),
                           ("cache", "缓存读")):
            fr = tk.Frame(pd, bg="#221A38")
            fr.pack(fill="x", padx=14, pady=2)
            tk.Label(fr, text=label, bg="#221A38", fg="#EDE6FF",
                     width=6, font=("Microsoft YaHei UI", 9)).pack(side="left")
            v = tk.StringVar(value=str(rates.get(key, "") or ""))
            tk.Entry(fr, textvariable=v, width=10).pack(side="left")
            vars_[key] = v
        fr2 = tk.Frame(pd, bg="#221A38")
        fr2.pack(fill="x", padx=14, pady=2)
        tk.Label(fr2, text="币种", bg="#221A38", fg="#EDE6FF", width=6,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        cur = tk.StringVar(value=self.cfg.get("currency", "¥"))
        tk.Entry(fr2, textvariable=cur, width=10).pack(side="left")

        def save():
            r = {}
            for k, v in vars_.items():
                try:
                    if v.get().strip():
                        r[k] = float(v.get())
                except ValueError:
                    pass
            self.cfg["rates"] = r or None
            self.cfg["currency"] = cur.get() or "¥"
            save_config(self.cfg)
            pd.destroy()

        tk.Button(pd, text="保存", command=save, bg="#8A63D9", fg="white",
                  activebackground="#A47BEA", relief="flat",
                  font=("Microsoft YaHei UI", 9, "bold"), width=8
                  ).pack(pady=8)

    # -- 主循环 -----------------------------------------------------------
    def _tick(self):
        self.tick_n += 1
        now = time.time()
        try:
            if self.tick_n % 30 == 0 or self.engine._scan():
                self.snap = self.engine.snapshot()
                if self.panel and self.panel.winfo_exists():
                    self._fill_panel(self.panel)
        except Exception:
            pass
        try:
            self._update_state(now)
            self._animate(now)
        except Exception:
            pass
        self.root.after(80, self._tick)

    def _update_state(self, now):
        s = self.snap
        rate = s["rate60"]
        idle = now - s["last_act"]
        old = self.state
        if rate > 30_000:
            self.state = "sweat"
        elif idle < 45:
            self.state = "working"
        elif idle > 600:
            self.state = "sleep"
        else:
            self.state = "idle"
        if self.state != old:
            self._on_state_change(old)

        # 里程碑庆祝
        t = s["today"]["in"] + s["today"]["out"]
        m = t // 100_000
        if m >= 1 and m not in self.milestone_done:
            self.milestone_done.add(m)
            self.party_until = now + 4
            self._say(f"今天已经 {m * 10}万 tokens 啦！\n主人工作好认真 ⭐", 5)
            ms = load_state()
            ms["milestone_day"] = datetime.now().strftime("%Y-%m-%d")
            ms["milestones"] = sorted(self.milestone_done)[-20:]
            save_state(ms)

        # 预测气泡（干活中且速率显著，每 5 分钟最多一次）
        if (self.state in ("working", "sweat")
                and now - getattr(self, "last_predict", 0) > 300
                and now > self.bubble_until):
            txt = predict_text(s["rate15"])
            if txt:
                self.last_predict = now
                self._say(txt, 6)

        # 闲聊气泡
        if (self.cfg.get("chirp", True) and now > self.bubble_until
                and self.state in ("idle", "sleep")
                and now - self.last_chirp > 420):
            self.last_chirp = now
            t_ = s["today"]
            lines = [
                f"今天已吃 {compact(t_['in'] + t_['out'])} tokens…还真是能吃呢",
                f"缓存命中 {100 * t_['cread'] // max(1, t_['in'])}%，哼、勉强及格",
                "哈？！我、我才不是特意在帮你数 token 呢…",
                "喂，agent 又在乱花钱了哦，笨蛋主人！",
                "学习要适度啊，熬通宵的话我可不管你",
                "Token 咔嚓咔嚓…啊、没、没在偷吃！",
            ]
            self._say(secrets.choice(lines), 5)

        if self.bubble_until and now > self.bubble_until:
            self._hide_bubble()

        # 今日标签
        if self.cfg.get("show_today", True):
            t_ = s["today"]
            self.canvas.itemconfig(
                self.today_label,
                text=f"今日 {compact(t_['in'] + t_['out'])}")
        else:
            self.canvas.itemconfig(self.today_label, text="")

        # 托盘悬停标题（30 秒同步一次）
        if now - getattr(self, "_tray_title_at", 0) > 30:
            self._tray_title_at = now
            td = s["today"]
            self._set_tray_title(
                f"{APP_NAME} · 今日 {compact(td['in'] + td['out'])} tokens"
                f" · {td['req']} 次请求")

    def _on_state_change(self, old):
        c = self.canvas
        c.itemconfig(self.sweat, state="normal" if self.state == "sweat"
                     else "hidden")

    def _animate(self, now):
        c = self.canvas
        state = "party" if now < self.party_until else self.state
        speed = {"working": 2.2, "sweat": 3.0, "party": 3.4,
                 "idle": 1.0, "sleep": 0.5}[state]
        phase = now * speed
        # 上下浮动 + 轻微左右摇摆
        dy = int(2.2 * math.sin(phase * 2.2)) - self._last_dy
        self._last_dy += dy
        dx = int(1.2 * math.sin(phase * 0.9)) - self._last_dx
        self._last_dx += dx
        for item in self.pet_items + [self.today_label, self.sweat]:
            try:
                c.move(item, dx, dy)
            except Exception:
                pass
        # 庆祝星星
        if state == "party":
            blink_on = int(now * 6) % 2 == 0
            for i, sp in enumerate(self.sparks):
                c.itemconfig(sp, state="normal" if (blink_on ^ (i % 2))
                             else "hidden")
        elif self.party_until:
            self.party_until = 0
            for sp in self.sparks:
                self._hide(sp)
        # Zzz
        if self.state == "sleep":
            off = int(now * 2) % 3
            for i, z in enumerate(self.zzz):
                c.itemconfig(z, state="normal" if i <= off else "hidden")
                c.coords(z, 168 + i * 8, 92 - off * 6 - i * 13)
        else:
            for z in self.zzz:
                self._hide(z)
        # 摸头小心心飘起
        if self.hearts_until > now:
            for i, h in enumerate(self.hearts):
                prog = (now - self.heart_start - i * 0.18) / 0.9
                if prog <= 0:
                    continue
                if prog >= 1:
                    self._hide(h)
                    continue
                hx, hy = self.heart_pos[i]
                c.itemconfig(h, state="normal")
                c.coords(h, self._heart(hx, hy - 34 * prog, 7 - 3 * prog))
        elif self.heart_pos:
            for h in self.hearts:
                self._hide(h)
            self.heart_pos = []


# ---------------------------------------------------------------- CLI 报告

def cli_report(engine):
    s = engine.snapshot()
    t, lt = s["today"], s["lifetime"]
    print(f"\n🐱 {APP_NAME} · Token 报告  {datetime.now():%Y-%m-%d %H:%M}")
    print(f"今天   输入 {compact(t['in'] - t['cread']):>8}  缓存读 "
          f"{compact(t['cread']):>8}  输出 {compact(t['out']):>8}"
          f"  合计 {compact(t['in'] + t['out']):>8}  ({t['req']} 次请求)")
    cost, sym = engine.cost_models(s["today_models"])
    if cost is not None:
        print(f"       估算成本 ≈ {sym}{cost:.2f}")
    wk = s["week"]
    print(f"近7天  合计 {compact(wk['in'] + wk['out']):>8}  ({wk['req']} 次请求)")
    print("\n近7天明细:")
    for i in range(6, -1, -1):
        d0 = (datetime.now().replace(hour=0, minute=0, second=0,
                                     microsecond=0) - timedelta(days=i))
        label = d0.strftime("%m-%d")
        b = s["days"].get(label)
        if not b:
            continue
        print(f"  {label}  纯输入 {compact(b['in'] - b['cread']):>8}  缓存 "
              f"{compact(b['cread']):>8}  输出 {compact(b['out']):>7}"
              f"  合计 {compact(b['in'] + b['out']):>8}  ({b['req']} 次)")
    print("\n按模型:")
    for model, b in sorted(s["models"].items(),
                           key=lambda kv: -(kv[1]["in"] + kv[1]["out"])):
        print(f"  {model:<28} 合计 {compact(b['in'] + b['out']):>8}"
              f"  ({b['req']} 次)")
    print(f"\n🏆 累计  合计 {compact(lt['in'] + lt['out'])} tokens · "
          f"{lt['req']} 次请求")
    print()


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--cli", action="store_true", help="打印统计报告后退出")
    ap.add_argument("--audit", action="store_true", help="数据自检报告")
    ap.add_argument("--debug", action="store_true", help="打印窗口坐标")
    ap.add_argument("--pos", default=None, help="初始位置 x,y（如 200,300）")
    ap.add_argument("--data-dir", default=None,
                    help="覆盖 model-io 数据目录（测试用，不写注册表）")
    args = ap.parse_args()

    if args.data_dir:
        engine = Engine(args.data_dir, persistent=False)
    else:
        engine = Engine(str(ROLL_DIR))

    if args.cli:
        cli_report(engine)
        return
    if args.audit:
        a = engine.audit()
        ok = a["engine_records"] + a["dedup_skipped"] == a["expected"]
        print(f"\n🔍 {APP_NAME} · 数据自检")
        print(f"  文件 {a['files']} 个 · 总行 {a['rows']} · 解析失败 "
              f"{a['parse_fail']} · 无用量行 {a['no_usage']}")
        print(f"  应计记录 {a['expected']} = 引擎 {a['engine_records']}"
              f" + 去重跳过 {a['dedup_skipped']}")
        print(f"  核对结果: {'✓ 一致，账目无误' if ok else '✗ 不一致！'}")
        print(f"  累计账本 {a['lifetime_req']} 次请求（注册表独立记账）")
        print()
        return

    cfg = load_config()
    if args.pos:
        try:
            px, py = args.pos.split(",")
            cfg["pos"] = [int(px), int(py)]
        except ValueError:
            pass
    root = tk.Tk()
    app = PetApp(root, engine, cfg)
    if args.debug:
        root.after(400, lambda: print(
            f"PETPOS {root.winfo_x()} {root.winfo_y()} {W} {H}", flush=True))
    root.mainloop()


if __name__ == "__main__":
    main()
