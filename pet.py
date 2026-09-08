"""zcode-pet: 零依赖桌面宠物（Windows / Python 标准库 tkinter）。

一只趴在屏幕右下角的橘猫，通过轮询 ~/.zcode/pet/events.jsonl 感知 zcode：
  收到任务 → 敲键盘状"工作中" / 任务完成 → 跳起来提醒 / 需要确认 → 摇铃提醒
  / 工具出错 → 懵 / 久等无事 → 睡觉。

用法:
  python pet.py --install     安装 zcode hooks（写入 ~/.zcode/cli/config.json）
  python pet.py --uninstall   移除 hooks
  python pet.py --download <id|url> [--proxy http://host:port]
                               从 codex-pets.net 下载像素宠物并设为当前形象
  python pet.py --list [关键词] 浏览社区宠物
  python pet.py --pet <id>    切换已下载的形象；--pet vector 切回内置橘猫
  python pet.py               启动桌宠

交互: 拖拽移动 / 单击撸猫 / 右键菜单(测试提醒·静音·换形象·大小·退出)
"""
import json
import math
import random
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).parent
PETS_DIR = HERE / "pets"

# ---------- 纯逻辑部分（无 tkinter，可被测试导入） ----------

PET_DIR = Path.home() / ".zcode" / "pet"
EVENTS_FILE = PET_DIR / "events.jsonl"
CONFIG_PATH = Path.home() / ".zcode" / "cli" / "config.json"
HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PermissionRequest", "PostToolUseFailure", "Stop"]

# 事件 -> (状态, 气泡文本 or None=不弹气泡)
EVENT_MAP = {
    "SessionStart":       ("greet",     "我上线啦 👋"),
    "UserPromptSubmit":   ("working",   "收到任务，干活中…"),
    "PermissionRequest":  ("attention", "⚠️ zcode 等你确认"),
    "PostToolUseFailure": ("oops",      "💢 有个工具出错了"),
    "Stop":               ("done",      "✅ 任务完成！"),
}
TEMP_STATES = ("greet", "done", "attention", "oops")  # 几秒后自动回 idle
TEMP_STATE_SECONDS = 6.0
WORKING_TIMEOUT = 15 * 60      # working 无事件 15 分钟视为中断
IDLE_TO_SLEEP = 5 * 60         # idle 5 分钟入睡


def read_new_events(path: Path, offset: int):
    """从 offset 读新增完整 JSON 行，返回 (events, new_offset)。
    不完整的尾行（无换行符）留到下次，避免读到半行。"""
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0
    if size < offset:  # 文件被轮转/清空，从头读
        offset = 0
    if size == offset:
        return [], offset
    events = []
    with open(path, "rb") as f:  # 二进制模式：offset 为字节，Windows 换行不影响
        f.seek(offset)
        data = f.read()
        complete = data.rfind(b"\n") + 1  # 0 表示没有完整行
        offset += complete
        for line in data[:complete].splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line).get("event")
            except ValueError:
                continue
            if ev in EVENT_MAP:
                events.append(ev)
    return events, offset


def next_state(state: str, last_event_ts: float, now: float):
    """按时间推导状态的自动流转（事件驱动的流转见 on_event）。"""
    age = now - last_event_ts
    if state == "working" and age > WORKING_TIMEOUT:
        return "idle"
    if state == "idle" and age > IDLE_TO_SLEEP:
        return "sleep"
    return state


def hook_config(python_exe: str, hook_script: str) -> dict:
    def cmd(event):
        return {"type": "process", "command": python_exe,
                "args": [hook_script, "--event", event], "timeoutMs": 5000}
    return {"enabled": True,
            "events": {ev: [{"hooks": [cmd(ev)]}] for ev in HOOK_EVENTS}}


def merge_hooks(cfg_path: Path, python_exe: str, hook_script: str) -> dict:
    """把 hooks 合并进 zcode 配置（保留原有其它键），返回写入后的配置。"""
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except ValueError:
            backup = cfg_path.with_suffix(".json.bak-pet")
            backup.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
            cfg = {}
    cfg["hooks"] = hook_config(python_exe, hook_script)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def remove_hooks(cfg_path: Path) -> dict:
    """仅当 hooks 指向本目录的 pet_hook.py 时才移除，动过手的配置不碰。"""
    if not cfg_path.exists():
        return {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    hooks = json.dumps(cfg.get("hooks", {}))
    if "pet_hook" not in hooks:
        return cfg
    cfg.pop("hooks", None)
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def demo_event(event: str) -> None:
    """手工注入一个事件（测试提醒用）。"""
    subprocess.Popen([sys.executable, str(Path(__file__).parent / "pet_hook.py"),
                      "--event", event],
                     creationflags=0x08000000)  # CREATE_NO_WINDOW


# ---------- codex-pets.net 像素宠物支持 ----------

SITE = "https://codex-pets.net"
CELL_W, CELL_H, COLS = 192, 208, 8  # 官方图集规格: 每帧 192x208, 8 列

# (动画id, 行, 帧数)，来自 codex-pets.net 前端源码的动画定义
ANIMS_V1 = [
    ("idle", 0, 6), ("running-right", 1, 8), ("running-left", 2, 8),
    ("waving", 3, 4), ("jumping", 4, 5), ("failed", 5, 8),
    ("waiting", 6, 6), ("running", 7, 6), ("review", 8, 6),
]
ANIMS_V2 = [("idle", 0, 7)] + ANIMS_V1[1:] + [("look-right-side", 9, 8), ("look-left-side", 10, 8)]

# zcode-pet 状态 -> (codex-pets 动画id, 播放速度倍率)
STATE_ANIM = {
    "idle": ("idle", 1.0),
    "working": ("running", 1.2),
    "done": ("jumping", 1.5),
    "greet": ("waving", 1.0),
    "attention": ("waving", 1.3),
    "oops": ("failed", 1.0),
    "sleep": ("waiting", 0.35),
}


def pet_id_from(text: str) -> str:
    """接受纯 id 或分享页 URL（https://codex-pets.net/#/pets/non0）。"""
    if "/" not in text:
        return text.strip()
    return text.rstrip("/").split("/")[-1].split("#")[-1].split("?")[0]


def _http_get(url: str, proxy: str = None) -> bytes:
    handlers = []
    proxies = {"http": proxy, "https": proxy} if proxy else urllib.request.getproxies()
    handlers.append(urllib.request.ProxyHandler(proxies))
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) zcode-pet/1.0",
    })
    with urllib.request.build_opener(*handlers).open(req, timeout=30) as r:
        return r.read()


def list_pets(query: str = "", proxy: str = None) -> list:
    url = f"{SITE}/api/pets?limit=20" + (f"&search={urllib.parse.quote(query)}" if query else "")
    return json.loads(_http_get(url, proxy)).get("pets", [])


def download_pet(ident: str, proxy: str = None) -> Path:
    """下载宠物图集并切成逐帧 PNG（Pillow 仅在下载时使用，运行时纯标准库）。"""
    pid = pet_id_from(ident)
    meta = json.loads(_http_get(f"{SITE}/api/pets/{pid}", proxy))["pet"]
    version = meta.get("spriteVersionNumber", 1)
    webp = _http_get(meta["spritesheetUrl"], proxy)

    from PIL import Image  # 下载期依赖
    import io
    sheet = Image.open(io.BytesIO(webp)).convert("RGBA")
    pdir = PETS_DIR / pid
    fdir = pdir / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    anims = ANIMS_V2 if version == 2 else ANIMS_V1
    for anim, row, frames in anims:
        for f in range(frames):
            box = (f * CELL_W, row * CELL_H, (f + 1) * CELL_W, (row + 1) * CELL_H)
            sheet.crop(box).save(fdir / f"{anim}_{f}.png")
    (pdir / "meta.json").write_text(json.dumps({
        "id": pid, "name": meta.get("displayName", pid), "kind": meta.get("kind"),
        "version": version, "anims": {a: [row, n] for a, row, n in anims},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    set_current_pet(pid)
    return pdir


def set_current_pet(pid: str) -> None:
    PETS_DIR.mkdir(exist_ok=True)
    (PETS_DIR / "current.txt").write_text(pid, encoding="utf-8")


# 大小档位；tk 的 zoom/subsample 只有整数倍，用分数组合出任意档
SIZES = [0.5, 0.75, 1.0, 1.5, 2.0]


def zoom_factors(s: float):
    """缩放比例 -> (num, den)，PhotoImage.zoom(num).subsample(den) 即最近邻缩放。"""
    f = Fraction(s).limit_denominator(4)
    return max(1, f.numerator), max(1, f.denominator)


def current_pet() -> str | None:
    try:
        pid = (PETS_DIR / "current.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return pid if pid and (PETS_DIR / pid / "meta.json").exists() else None


# ---------- 桌宠 UI ----------

import tkinter as tk  # noqa: E402
import tkinter.font as tkfont  # noqa: E402

W, H = 190, 205           # 画布：上方留给气泡，猫在底部
TRANSPARENT = "#010203"  # Windows 透明色：此颜色像素完全穿透
GROUND = H - 18

# 橘猫配色
C_BODY, C_LINE = "#FFD9A0", "#7A4E22"
C_STRIPE, C_EAR = "#E8A25E", "#FFB3C1"
C_BLUSH, C_EYE = "#FFB9C0", "#4A3220"

PAT_WORDS = ["喵~", "嘿嘿", "摸摸头？", "加油鸭！", "喵呜♪"]
WORKING_DOTS = ["🐾 工作中", "🐾 工作中·", "🐾 工作中··", "🐾 工作中···"]


class Pet:
    def __init__(self, pet_id=None):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.pid = pet_id or current_pet()  # None = 内置矢量橘猫
        if self.pid == "vector":
            self.pid = None
        try:
            s = float((PETS_DIR / "size.txt").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            s = 1.0
        self.scale = s if s in SIZES else 1.0
        self.frames = {}                # anim -> [PhotoImage]（懒加载）
        self.anim_pos = 0.0
        self._apply_dims()

        self.root = tk.Tk()
        self.root.title("zcode-pet")
        self.root.overrideredirect(True)             # 无边框
        self.root.attributes("-topmost", True)       # 置顶
        self.root.attributes("-transparentcolor", TRANSPARENT)  # 透明穿透
        self.root.config(bg=TRANSPARENT)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{self.W}x{self.H}+{sw - self.W - 40}+{sh - self.H - 60}")

        self.cv = tk.Canvas(self.root, width=self.W, height=self.H, bg=TRANSPARENT,
                            highlightthickness=0, bd=0)
        self.cv.pack()

        self.state, self.muted = "idle", False
        try:
            # 启动只看新事件：从文件末尾开始，不重放历史（否则每次启动闪旧提醒）
            self.offset = EVENTS_FILE.stat().st_size
        except OSError:
            self.offset = 0
        self.last_event_ts, self.state_ts, self.phase = time.time(), time.time(), 0
        self.bubble_until, self.pat_until, self.blink_at = 0, 0, time.time() + 2
        self.bubble_text = None
        self._drag = None
        self._menu = None
        self._ids = []          # 宠物图元 id
        self._bubble_ids = []
        self._parts = {}        # 需要逐帧改动的图元

        self.root.report_callback_exception = self._log_cb_error
        self.root.after(120, self.tick)
        self.root.after(500, self.poll)
        self.bind_events()

    def _log_cb_error(self, *exc):
        try:
            import traceback
            (HERE / "pet_error.log").open("a", encoding="utf-8").write(
                traceback.format_exception(*exc)[-1])
        except Exception:
            pass

    # ---- 事件与状态 ----

    def poll(self):
        # ponytail: 事件文件只增不删，>1MB 时清空轮转（事件是易失的）
        try:
            if EVENTS_FILE.stat().st_size > 1_000_000:
                EVENTS_FILE.write_text("", encoding="utf-8")
                self.offset = 0
        except OSError:
            pass
        events, self.offset = read_new_events(EVENTS_FILE, self.offset)
        for ev in events:
            self.on_event(ev)
        self.root.after(500, self.poll)

    def on_event(self, ev: str):
        state, text = EVENT_MAP[ev]
        now = time.time()
        self.state, self.last_event_ts, self.state_ts = state, now, now
        if text:
            self.say(text)
        if state == "done" and not self.muted:
            self.beep("asterisk")
        elif state == "attention" and not self.muted:
            self.beep("exclamation")

    def beep(self, kind):
        try:
            import winsound
            winsound.MessageBeep({"asterisk": winsound.MB_ICONASTERISK,
                                  "exclamation": winsound.MB_ICONEXCLAMATION}[kind])
        except Exception:
            pass

    def say(self, text, seconds=None):
        self.bubble_text = text
        self.bubble_until = time.time() + (seconds if seconds is not None
                                           else TEMP_STATE_SECONDS)

    # ---- 绘制 ----

    def tick(self):
        now = time.time()
        self.phase += 1
        self.state = next_state(self.state, self.last_event_ts, now)

        if self.state in TEMP_STATES and now - self.state_ts > TEMP_STATE_SECONDS:
            self.state, self.state_ts = "idle", now
        if self.state == "working":
            self.bubble_text = WORKING_DOTS[self.phase // 8 % 4]
            self.bubble_until = now + 2  # 常驻：working 状态期间每帧续期
        elif self.bubble_text and now > self.bubble_until:
            self.bubble_text = None

        self.draw()
        self.root.after(120, self.tick)

    def draw(self):
        cv = self.cv
        for i in self._ids + self._bubble_ids:
            cv.delete(i)
        self._ids, self._bubble_ids, self._parts = [], [], {}
        if self.pid:
            self.draw_sprite()
        else:
            self.draw_vector()
        self.draw_overlays()
        if not self.pid and self.scale != 1.0:
            self.cv.scale("all", 0, 0, self.scale, self.scale)
        self.root.title(f"zcode-pet [{self.state}]")

    # ---- codex-pets sprite 渲染 ----

    def draw_sprite(self):
        anim, speed = STATE_ANIM.get(self.state, ("idle", 1.0))
        if self.pat_until > time.time() and anim != "jumping":
            anim, speed = "jumping", 1.2
        imgs = self._anim_frames(anim)
        if not imgs:
            return
        self.anim_pos = (self.anim_pos + speed) % len(imgs)
        self._ids.append(self.cv.create_image(
            self.DW / 2, self.GROUND, anchor="s", image=imgs[int(self.anim_pos)]))

    def _anim_frames(self, anim: str) -> list:
        if anim in self.frames:
            return self.frames[anim]
        meta = json.loads((PETS_DIR / self.pid / "meta.json").read_text(encoding="utf-8"))
        _, n = meta["anims"][anim]
        fdir = PETS_DIR / self.pid / "frames"
        try:
            imgs = [tk.PhotoImage(file=str(fdir / f"{anim}_{i}.png")) for i in range(n)]
        except tk.TclError:
            imgs = []
        if self.scale != 1.0:
            zn, zd = zoom_factors(self.scale)
            imgs = [im.zoom(zn).subsample(zd) for im in imgs]
        self.frames[anim] = imgs
        return imgs

    def _apply_dims(self):
        """真实窗口 = 基准 × 缩放。绘制空间：sprite=真实尺寸（图已缩放）；
        矢量猫=基准坐标（画完统一用 canvas.scale 变换，字体单独放大）。"""
        bw, bh = (200, 250) if self.pid else (W, H)
        self.W, self.H = round(bw * self.scale), round(bh * self.scale)
        if self.pid:
            self.DW, self.GROUND = self.W, self.H - round(14 * self.scale)
        else:
            self.DW, self.GROUND = bw, bh - 14

    def _set_scale(self, s: float):
        old_w, old_h = self.W, self.H
        self.scale = s
        (PETS_DIR / "size.txt").write_text(str(s), encoding="utf-8")
        self.frames = {}                # 缓存的帧是按旧缩放生成的
        self._apply_dims()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        # 右下角锚定，缩放时宠物不会往下钻出屏幕
        self.root.geometry(f"{self.W}x{self.H}+{x + old_w - self.W}+{y + old_h - self.H}")
        self.draw()

    def _switch_pet(self, pid: str):
        self.pid = None if pid == "vector" else pid
        self.frames = {}
        self._apply_dims()
        self.root.geometry(f"{self.W}x{self.H}")
        if self.pid:
            set_current_pet(self.pid)
        else:
            (PETS_DIR / "current.txt").unlink(missing_ok=True)

    # ---- 内置矢量橘猫 ----

    def draw_vector(self):
        cv, p = self.cv, self.phase
        G = self.GROUND

        # 呼吸/跳跃位移
        if self.state == "done":
            dy = -int(20 * abs(math.sin(p * 0.35)))
            bob = 0
        elif self.state == "working":
            dy, bob = 0, int(2.5 * math.sin(p * 0.5))
        elif self.state == "sleep":
            dy = bob = 0
        else:
            dy, bob = 0, int(1.5 * math.sin(p * 0.12))
        base_y = dy + bob
        sleeping = self.state == "sleep"
        patted = self.pat_until > time.time()

        def add(fn, *a, **kw):
            i = fn(*a, **kw)
            self._ids.append(i)
            return i

        o = dict(outline=C_LINE, width=2)

        # 尾巴（摇动）
        wag = math.sin(p * (0.4 if self.state == "working" else 0.15))
        tx, ty = 148 + wag * 6, G - 22 + wag * -8
        add(cv.create_line, 132, G - 14, tx, ty, smooth=True, width=7,
            fill=C_BODY, capstyle="round")
        add(cv.create_line, 132, G - 14, tx, ty, smooth=True, width=3,
            fill=C_LINE, capstyle="round")

        # 身体（趴猫椭圆）
        add(cv.create_oval, 34, G - 62 + base_y, 142, G + base_y,
            fill=C_BODY, **o)
        # 耳朵
        for sx in (0, 1):
            ex = 55 if sx == 0 else 121
            tipx = ex + (-8 if sx == 0 else 8)
            add(cv.create_polygon, ex - 10, G - 52 + base_y, tipx,
                G - 78 + base_y, ex + 10, G - 56 + base_y,
                fill=C_BODY, **o)
            add(cv.create_polygon, ex - 4, G - 55 + base_y, tipx,
                G - 71 + base_y, ex + 4, G - 57 + base_y,
                fill=C_EAR, outline="")
        # 头顶条纹
        for i, w in ((0, 10), (1, 14), (2, 10)):
            cx = 68 + i * 16
            add(cv.create_line, cx, G - 62 + base_y, cx + 2,
                G - 50 + base_y, width=4, fill=C_STRIPE, capstyle="round")
        # 前爪
        for cx in (66, 96):
            add(cv.create_oval, cx, G - 14 + base_y, cx + 24, G + base_y,
                fill=C_BODY, **o)
        # 眼睛
        ey = G - 38 + base_y
        if sleeping:
            for ex in (66, 100):
                add(cv.create_arc, ex, ey - 5, ex + 16, ey + 9, start=200,
                    extent=140, style="arc", outline=C_EYE, width=2)
        elif patted:
            for ex in (66, 100):
                add(cv.create_arc, ex, ey - 2, ex + 16, ey + 12, start=20,
                    extent=140, style="arc", outline=C_EYE, width=2)
        elif self._blink():
            for ex in (66, 100):
                add(cv.create_line, ex + 2, ey + 5, ex + 14, ey + 5,
                    width=2, fill=C_EYE, capstyle="round")
        else:
            for ex in (66, 100):
                add(cv.create_oval, ex, ey - 4, ex + 16, ey + 11,
                    fill=C_EYE, outline="")
        # 腮红
        for bx in (48, 116):
            add(cv.create_oval, bx, ey + 8, bx + 12, ey + 17, fill=C_BLUSH,
                outline="")
        # 嘴 ω（工作中/开心时张嘴）
        my = ey + 14
        if self.state in ("working", "done") or patted:
            add(cv.create_oval, 80, my - 2, 98, my + 9, fill="#E8748A", outline="")
        else:
            add(cv.create_arc, 79, my - 6, 90, my + 6, start=210, extent=120,
                style="arc", outline=C_EYE, width=2)
            add(cv.create_arc, 89, my - 6, 100, my + 6, start=30, extent=120,
                style="arc", outline=C_EYE, width=2)
        # 胡须
        for wx, dx in ((38, -14), (138, 14)):
            add(cv.create_line, wx, G - 42 + base_y, wx + dx, G - 46 + base_y,
                width=1, fill=C_LINE)
            add(cv.create_line, wx, G - 36 + base_y, wx + dx, G - 34 + base_y,
                width=1, fill=C_LINE)

    def draw_overlays(self):
        """撒花/爱心/Zzz/气泡——矢量猫与 sprite 模式共用。"""
        cv, p, G = self.cv, self.phase, self.GROUND
        patted = self.pat_until > time.time()

        def add(fn, *a, **kw):
            i = fn(*a, **kw)
            self._ids.append(i)
            return i

        if self.state == "sleep":
            for k in range(3):
                t = (p * 0.08 + k * 0.33) % 1.0
                zx, zy = 140 - k * 6 - t * 10, G - 70 - t * 26
                fs = 11 + k * 4
                add(cv.create_text, zx, zy, text="Z", font=("Segoe UI", fs, "bold"),
                    fill="#9B8AFB")

        if self.state == "done":
            for k in range(5):
                t = (p * 0.2 + k * 0.2) % 1.0
                hx = 45 + k * 25 + math.sin(k * 2.1) * 8
                hy = G - 95 - t * 45
                add(cv.create_text, hx, hy, text=random.choice("✨🎉💛"),
                    font=("Segoe UI Emoji", 10))

        if patted:
            t = 1 - (self.pat_until - time.time()) / 1.5
            add(cv.create_text, self.DW / 2 - t * 10, G - 80 - t * 28, text="❤",
                font=("Segoe UI Emoji", 11))

        if self.bubble_text:
            self._draw_bubble(self.bubble_text)

    def _blink(self) -> bool:
        now = time.time()
        if now > self.blink_at:
            self.blink_at = now + random.uniform(2.2, 4.5)
            self.blink_off = now + 0.18
        return getattr(self, "blink_off", 0) > now

    def _draw_bubble(self, text: str):
        cv = self.cv
        fs = max(8, round(10 * self.scale))
        font = ("Microsoft YaHei UI", fs)
        tw = tkfont.Font(font=font).measure(text) + 20
        th = max(18, round(26 * self.scale))
        bx = (self.DW - tw) / 2
        by = 4
        poly = cv.create_polygon(
            bx + 8, by, bx + tw - 8, by, bx + tw, by + th / 2,
            bx + tw - 8, by + th, bx + tw / 2 + 8, by + th,
            bx + tw / 2, by + th + 9, bx + tw / 2 - 8, by + th,  # 小尾巴
            bx + 8, by + th, bx, by + th / 2,
            fill="#FFFFFF", outline=C_LINE, width=1.5, smooth=True)
        txt = cv.create_text(self.DW / 2, by + th / 2, text=text, font=font, fill="#333333")
        self._bubble_ids += [poly, txt]

    # ---- 交互 ----

    def bind_events(self):
        cv = self.cv
        cv.bind("<Button-1>", self._press)
        cv.bind("<B1-Motion>", self._drag_move)
        cv.bind("<ButtonRelease-1>", self._release)
        cv.bind("<Button-3>", self._popup)

    def _press(self, e):
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), False)

    def _drag_move(self, e):
        # ponytail: 用屏幕坐标+每次重新锚定。窗口移动后 Windows 会补发"重算"的
        # 假 MOUSEMOVE（客户区坐标已变化），相对坐标方案会因此来回抽搐；
        # 屏幕坐标下静止指针 delta=0，天然免疫。
        if not self._drag:
            return
        px, py, wx, wy, moved = self._drag
        dx, dy = e.x_root - px, e.y_root - py
        if not moved and abs(dx) + abs(dy) <= 6:
            return
        self._drag = (e.x_root, e.y_root, wx + dx, wy + dy, True)
        if dx or dy:
            self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _release(self, e):
        if self._drag and not self._drag[4]:  # 未拖动 = 单击撸猫
            self.pat_until = time.time() + 1.5
            if self.state == "sleep":
                self.state, self.state_ts = "idle", time.time()
            if random.random() < 0.6:
                self.say(random.choice(PAT_WORDS), 2.0)
        self._drag = None

    def _popup(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="测试「任务完成」提醒", command=lambda: self.on_event("Stop"))
        m.add_command(label="测试「需要确认」提醒", command=lambda: self.on_event("PermissionRequest"))
        m.add_separator()
        var = tk.BooleanVar(value=self.muted)
        m.add_checkbutton(label="静音", variable=var,
                          command=lambda: setattr(self, "muted", var.get()))
        # 换形象：内置橘猫 + 已下载的 codex-pets
        pets_menu = tk.Menu(m, tearoff=0)
        pets_menu.add_command(label="🐱 内置橘猫",
                              command=lambda: self._switch_pet("vector"))
        for pdir in sorted(PETS_DIR.glob("*/meta.json")):
            try:
                name = json.loads(pdir.read_text(encoding="utf-8")).get("name", pdir.parent.name)
            except ValueError:
                continue
            pid = pdir.parent.name
            mark = " ✓" if pid == self.pid else ""
            pets_menu.add_command(label=f"{name} ({pid}){mark}",
                                  command=lambda pid=pid: self._switch_pet(pid))
        m.add_cascade(label="换形象", menu=pets_menu)
        # 大小：50%~200%，最近邻缩放（像素画不糊），选择持久化
        size_menu = tk.Menu(m, tearoff=0)
        sv = tk.DoubleVar(value=self.scale)
        for s in SIZES:
            size_menu.add_radiobutton(label=f"{int(s * 100)}%", value=s, variable=sv,
                                      command=lambda s=s: self._set_scale(s))
        m.add_cascade(label="大小", menu=size_menu)
        m.add_separator()
        m.add_command(label="退出", command=self.root.destroy)
        m.tk_popup(e.x_root, e.y_root)

    def run(self):
        self.root.mainloop()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="zcode-pet 桌面宠物")
    ap.add_argument("--install", action="store_true", help="安装 zcode hooks")
    ap.add_argument("--uninstall", action="store_true", help="移除 zcode hooks")
    ap.add_argument("--download", metavar="ID|URL", help="从 codex-pets.net 下载像素宠物")
    ap.add_argument("--list", nargs="?", const="", metavar="关键词", help="浏览社区宠物")
    ap.add_argument("--pet", metavar="ID|vector", help="切换当前形象")
    ap.add_argument("--proxy", metavar="URL", help="HTTP 代理（默认读 HTTP(S)_PROXY 环境变量）")
    a = ap.parse_args()

    if a.install:
        cfg = merge_hooks(CONFIG_PATH, sys.executable, str(HERE / "pet_hook.py"))
        print(f"hooks 已安装 -> {CONFIG_PATH}，事件: {', '.join(cfg['hooks']['events'])}")
        print("重启 zcode 会话后生效。启动桌宠: python pet.py")
    elif a.uninstall:
        remove_hooks(CONFIG_PATH)
        print(f"hooks 已从 {CONFIG_PATH} 移除")
    elif a.download:
        pdir = download_pet(a.download, a.proxy)
        print(f"已下载并设为当前形象: {pdir}")
        print("启动/重启桌宠查看: pythonw pet.py")
    elif a.list is not None:
        for p in list_pets(a.list, a.proxy):
            print(f"{p['id']:<16} {p['displayName']}  [{p.get('kind','')}] {', '.join(p.get('tags', [])[:4])}")
        print("下载: python pet.py --download <id>")
    elif a.pet:
        if a.pet == "vector":
            (PETS_DIR / "current.txt").unlink(missing_ok=True)
            print("当前形象: 内置橘猫")
        else:
            pid = pet_id_from(a.pet)
            assert (PETS_DIR / pid / "meta.json").exists(), f"未下载 {pid}，先 --download {pid}"
            set_current_pet(pid)
            print(f"当前形象: {pid}")
    else:
        Pet().run()


if __name__ == "__main__":
    main()
