"""zcode-pet: 零依赖桌面宠物（Windows / Python 标准库 tkinter）。

一只趴在屏幕右下角的像素宠物（形象来自 codex-pets.net），通过轮询
~/.zcode/pet/events.jsonl 感知 zcode：收到任务 → 巡逻干活 / 任务完成 →
跳起来提醒 / 等你审阅 / 闲时张望、睡觉。多个 zcode 会话并行时，每个会话
一台独立状态机再聚合显示——A 任务完成会提醒你"（B 还在继续）"。

用法:
  python pet.py --install     安装 zcode hooks（写入 ~/.zcode/cli/config.json）
  python pet.py --uninstall   移除 hooks
  python pet.py --download <id|url> [--proxy http://host:port]
                               从 codex-pets.net 下载像素宠物并设为当前形象
  python pet.py --list [关键词] 浏览社区宠物
  python pet.py --pet <id>    切换已下载的形象
  python pet.py               启动桌宠

交互: 拖拽移动 / 单击撸宠 / 右键菜单(测试提醒·静音·换形象·大小·退出)
"""
import json
import random
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

# 事件 -> 所属会话进入的状态（气泡文案按任务名动态拼，见 on_event）
EVENT_MAP = {
    "SessionStart":       "greet",
    "UserPromptSubmit":   "working",
    "PermissionRequest":  "attention",
    "PostToolUseFailure": "oops",
    "Stop":               "done",
}
TEMP_STATE_SECONDS = 6.0
STATE_TTL = {"glance": 2.5}  # 张望只持续一小会儿
WORKING_TIMEOUT = 15 * 60    # 会话 working 无事件 15 分钟视为中断
ATTENTION_TTL = 2 * 60       # 等审阅无后续事件，这么久后视为已处理
SESSION_TTL = 30 * 60        # 会话这么久无事件即剔除（没有 SessionEnd 事件可依赖）
IDLE_TO_SLEEP = 5 * 60       # 全部空闲 5 分钟入睡
REVIEW_FOLLOWUP_DELAY = 30     # done 收尾后多少秒追加"看看成果"
PATROL_SPEED = 60              # working 巡逻速度 px/s
PATROL_SETTLE = 3.0            # 拖拽放下后定住几秒再继续巡逻


def patrol_step(x: float, direction: int, speed: float, dt: float,
                x_min: int, x_max: int):
    """working 巡逻：推进窗口 x 并在 [x_min, x_max]（窗口左上角允许范围）内折返。
    direction: +1 右 / -1 左。多显示器下 x_min 可为负。"""
    x += direction * speed * dt
    if x > x_max:
        x, direction = x_max, -1
    elif x < x_min:
        x, direction = x_min, 1
    return x, direction


def monitor_span(x: int, y: int):
    """点 (x,y) 所在显示器的水平范围 (left, right)；查不到回退主屏。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        spans = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        def cb(h, dc, rc, lp):
            r = rc.contents
            spans.append((r.left, r.right))
            return True

        user32.EnumDisplayMonitors(None, None, cb, 0)
        for lo, hi in spans:
            if lo <= x < hi:
                return lo, hi
        return 0, user32.GetSystemMetrics(0)
    except Exception:
        return 0, 1920


def read_new_events(path: Path, offset: int):
    """从 offset 读新增完整 JSON 行，返回 (records, new_offset)。
    record = (event, sid, name)；旧格式无 sid 时为 ("", None)，全部并入默认会话。
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
                rec = json.loads(line)
                ev = rec.get("event")
            except ValueError:
                continue
            if ev in EVENT_MAP:
                events.append((ev, rec.get("sid") or "", rec.get("name")))
    return events, offset


AGG_PRIORITY = ("attention", "working", "oops", "done", "greet")


def session_next_state(state: str, last_ts: float, state_ts: float, now: float) -> str:
    """单会话状态自动流转：瞬态到期回落、working 超时判死、审阅超时视为已处理。"""
    if state == "working" and now - last_ts > WORKING_TIMEOUT:
        return "idle"
    if state == "attention" and now - state_ts > ATTENTION_TTL:
        return "idle"   # ponytail: 没有"已批准"事件，只能超时后假定已处理
    if state == "oops" and now - state_ts > TEMP_STATE_SECONDS:
        return "working"  # 工具失败不终止任务，闪完继续干
    if state in ("greet", "done") and now - state_ts > TEMP_STATE_SECONDS:
        return "idle"
    return state


def aggregate_state(sessions: dict) -> str:
    """多会话 -> 宠物全局状态：按 AGG_PRIORITY 取最要紧的，都闲着才 idle。
    有人等审阅最优先（需要人操作），还有人在干就保持 working。"""
    live = {s["state"] for s in sessions.values()}
    return next((st for st in AGG_PRIORITY if st in live), "idle")


def hook_config(python_exe: str, hook_script: str) -> dict:
    def cmd(event):
        return {"type": "process", "command": python_exe,
                "args": [hook_script, "--event", event,
                         "--sid", "${CLAUDE_SESSION_ID}"],  # zcode 展开会话 id
                "timeoutMs": 5000}
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
    "working": ("running", 1.2),        # 兜底；sprite 巡逻时按方向取 running-right/left
    "done": ("jumping", 1.5),
    "greet": ("waving", 1.0),
    "attention": ("review", 1.0),
    "oops": ("failed", 1.0),
    "sleep": ("waiting", 0.35),
    "review_wait": ("review", 1.0),
    "glance": ("idle", 1.0),            # 实际动画由 _glance_anim 覆盖（v2 look 行）
}


def remove_pet(pid: str) -> None:
    """删除皮肤目录；若删的是当前形象，自动切到默认形象（没有则清空选择）。"""
    import shutil
    was_current = current_pet() == pid  # 必须在删除前判断：删后 meta 不存在，current_pet() 会读成 None
    shutil.rmtree(PETS_DIR / pid, ignore_errors=True)
    if was_current:
        nxt = bundled_default()
        if nxt:
            set_current_pet(nxt)
        else:
            (PETS_DIR / "current.txt").unlink(missing_ok=True)


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

SETTINGS_FILE = PETS_DIR / "settings.json"
DEFAULT_SETTINGS = {"scale": 1.0, "patrol": True, "muted": False, "x": None, "y": None}


def load_settings() -> dict:
    try:
        d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if not d:  # 旧版散文件一次性迁移（size.txt / patrol.txt）
        try:
            d["scale"] = float((PETS_DIR / "size.txt").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
        try:
            d["patrol"] = (PETS_DIR / "patrol.txt").read_text(encoding="utf-8").strip() != "0"
        except OSError:
            pass
    out = dict(DEFAULT_SETTINGS)
    out.update({k: d[k] for k in DEFAULT_SETTINGS if k in d})
    if out["scale"] not in SIZES:
        out["scale"] = 1.0
    out["patrol"], out["muted"] = bool(out["patrol"]), bool(out["muted"])
    return out


def save_settings(**kw) -> dict:
    s = load_settings()
    s.update(kw)
    SETTINGS_FILE.parent.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    return s


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


# 内置皮肤（随仓库分发，开箱即用）；DEFAULT_PET 为未选择时的默认形象
DEFAULT_PET = "xm-cat"


def bundled_default() -> str | None:
    """未选择形象时的兜底：优先 DEFAULT_PET，否则第一只内置/已下载皮肤。"""
    if (PETS_DIR / DEFAULT_PET / "meta.json").exists():
        return DEFAULT_PET
    metas = sorted(PETS_DIR.glob("*/meta.json"))
    return metas[0].parent.name if metas else None


# ---------- 桌宠 UI ----------

import tkinter as tk  # noqa: E402
import tkinter.font as tkfont  # noqa: E402
import tkinter.messagebox  # noqa: E402,F401（无皮肤时引导弹窗用）

TRANSPARENT = "#010203"  # Windows 透明色：此颜色像素完全穿透

PAT_WORDS = ["嘿~", "嘿嘿", "摸摸头？", "加油鸭！", "♪"]
def working_bubble(patrol_on: bool, phase: int, names: str = "") -> str:
    """working 常驻气泡：点名在跑的任务，开巡逻=巡逻中，关=工作中，点数循环。"""
    who = f"{names} " if names else ""
    base = f"🐾 {who}{'巡逻中' if patrol_on else '工作中'}"
    return base + "·" * (phase // 8 % 3)


class Pet:
    def __init__(self, pet_id=None):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.pid = pet_id or current_pet() or bundled_default()
        if not self.pid:
            raise SystemExit("没有可用的宠物形象，先: python pet.py --download non0")
        self.settings = load_settings()
        self.scale = self.settings["scale"]
        self.frames = {}                # anim -> [PhotoImage]（懒加载）
        self.anim_pos = 0.0
        self._apply_dims()
        self._refresh_skin_meta()

        self.root = tk.Tk()
        self.root.title("zcode-pet")
        self.root.overrideredirect(True)             # 无边框
        self.root.attributes("-topmost", True)       # 置顶
        self.root.attributes("-transparentcolor", TRANSPARENT)  # 透明穿透
        self.root.config(bg=TRANSPARENT)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        # 恢复上次位置：须落在某块显示器内（防分辨率/布局变更后跑到屏外）
        px, py = self.settings["x"], self.settings["y"]
        ok = px is not None
        if ok:
            lo, hi = monitor_span(px + self.W // 2, py)
            ok = lo <= px + self.W // 2 < hi and -200 <= py <= sh
        self.root.geometry(f"{self.W}x{self.H}+{px if ok else sw - self.W - 40}"
                           f"+{py if ok else sh - self.H - 60}")

        self.cv = tk.Canvas(self.root, width=self.W, height=self.H, bg=TRANSPARENT,
                            highlightthickness=0, bd=0)
        self.cv.pack()

        self.state = "idle"
        self.sessions = {}         # sid -> {sid,name,state,last_ts,state_ts} 每会话独立状态机
        self.muted = self.settings["muted"]
        self.patrol_on = self.settings["patrol"]
        self.patrol_dir = random.choice((1, -1))
        self._polls = 0
        self.patrol_pause_until = 0.0
        self.review_followup_at = 0.0
        self.next_glance_at = time.time() + random.uniform(20, 60)
        try:
            # 启动只看新事件：从文件末尾开始，不重放历史（否则每次启动闪旧提醒）
            self.offset = EVENTS_FILE.stat().st_size
        except OSError:
            self.offset = 0
        self.last_event_ts, self.state_ts, self.phase = time.time(), time.time(), 0
        self.bubble_until, self.pat_until = 0, 0
        self.bubble_text = None
        self.bubble_is_notify = False   # 当前气泡是否通知类（✅/📋等，working 常驻气泡让位）
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

    def _refresh_skin_meta(self):
        """读取当前皮肤的动画清单（v1 缺 look 行 → 张望自动禁用）。"""
        self._skin_anims = set()
        self._glance_anim = None
        if not self.pid:
            return
        try:
            anims = json.loads((PETS_DIR / self.pid / "meta.json")
                               .read_text(encoding="utf-8"))["anims"]
            self._skin_anims = set(anims)
        except (OSError, ValueError, KeyError):
            pass

    @property
    def _look_anims(self):
        return [a for a in ("look-right-side", "look-left-side")
                if a in self._skin_anims]

    # ---- 事件与状态 ----

    def _save(self):
        """落盘当前设置（含窗口位置）。"""
        save_settings(x=self.root.winfo_x(), y=self.root.winfo_y())

    def poll(self):
        self._polls += 1
        if self._polls % 20 == 0:  # 每 10s 兜底存一次位置，防进程被杀丢失
            self._save()
        # ponytail: 事件文件只增不删，>1MB 时清空轮转（事件是易失的）
        try:
            if EVENTS_FILE.stat().st_size > 1_000_000:
                EVENTS_FILE.write_text("", encoding="utf-8")
                self.offset = 0
        except OSError:
            pass
        # 无 SessionEnd 事件：超久无动静的会话直接剔除，防 sessions 无限增长
        now = time.time()
        self.sessions = {k: s for k, s in self.sessions.items()
                         if now - s["last_ts"] < SESSION_TTL}
        events, self.offset = read_new_events(EVENTS_FILE, self.offset)
        for ev, sid, name in events:
            self.on_event(ev, sid, name)
        self.root.after(500, self.poll)

    def _display_name(self, s: dict) -> str:
        """会话显示名：项目目录名；同目录并行多个会话时加 sid 前缀区分。"""
        n = s["name"] or ""
        if n and sum(1 for t in self.sessions.values() if (t["name"] or "") == n) > 1:
            n = f"{n}·{s['sid'][:4]}"
        return n

    def on_event(self, ev: str, sid: str = "", name: str = None):
        now = time.time()
        self.last_event_ts = now
        s = self.sessions.setdefault(
            sid, {"sid": sid, "name": None, "state": "idle",
                  "last_ts": now, "state_ts": now})
        if name:
            s["name"] = name
        s["state"], s["last_ts"], s["state_ts"] = EVENT_MAP[ev], now, now
        self._glance_anim = None       # 新事件打断张望
        who = self._display_name(s)
        tag = f"{who} " if who else ""
        others = [self._display_name(t) for t in self.sessions.values()
                  if t is not s and t["state"] == "working"]
        if ev == "Stop":
            tail = (f"（{'、'.join(o for o in others if o)} 还在继续）"
                    if others else "")
            self.say(f"✅ {tag}任务完成！{tail}")
            if not self.muted:
                self.beep("asterisk")
        elif ev == "PermissionRequest":
            self.say(f"📋 {tag}等你审阅")
            if not self.muted:
                self.beep("exclamation")
        elif ev == "SessionStart":
            self.say(f"👋 {tag}上线")
        elif ev == "PostToolUseFailure":
            self.say(f"💢 {tag}有个工具出错了")
        # UserPromptSubmit 不弹气泡：working 常驻气泡由 tick 点名生成
        self.state, self.state_ts = aggregate_state(self.sessions), now

    def beep(self, kind):
        try:
            import winsound
            winsound.MessageBeep({"asterisk": winsound.MB_ICONASTERISK,
                                  "exclamation": winsound.MB_ICONEXCLAMATION}[kind])
        except Exception:
            pass

    def say(self, text, seconds=None):
        self.bubble_text = text
        self.bubble_is_notify = True
        self.bubble_until = time.time() + (seconds if seconds is not None
                                           else TEMP_STATE_SECONDS)

    # ---- 绘制 ----

    def tick(self):
        now = time.time()
        self.phase += 1

        # 1) 每个会话各自流转（瞬态到期/超时）；全局收工时预约"看看成果"
        for s in self.sessions.values():
            prev = s["state"]
            s["state"] = session_next_state(prev, s["last_ts"], s["state_ts"], now)
            if s["state"] != prev:
                s["state_ts"] = now
                if prev == "done" and aggregate_state(self.sessions) == "idle":
                    self.review_followup_at = now + REVIEW_FOLLOWUP_DELAY

        # 2) 聚合出全局状态；全部空闲时维持 glance/review_wait/sleep 自转
        agg = aggregate_state(self.sessions)
        if agg != "idle":
            if self.state != agg:
                self.state, self.state_ts, self._glance_anim = agg, now, None
        else:
            if self.state == "glance" and now - self.state_ts > STATE_TTL["glance"]:
                self._glance_anim = None
                self.state, self.state_ts = "idle", now
            elif self.state in AGG_PRIORITY:    # 聚合已空闲，活动态直接落幕
                self.state, self.state_ts = "idle", now
            if self.state == "review_wait" and now - self.state_ts > TEMP_STATE_SECONDS:
                self.state, self.state_ts = "idle", now
            # ponytail: 不做"用户无操作"检测（stdlib 无全局输入监听），固定延迟代替
            if self.state == "idle" and self.review_followup_at and now >= self.review_followup_at:
                self.review_followup_at = 0
                self._enter_temp("review_wait", "看看我的成果？")
            elif (self.state == "idle" and self._glance_anim is None
                    and not self.bubble_text and now >= self.next_glance_at
                    and self._look_anims):
                self.next_glance_at = now + random.uniform(20, 60)
                self._glance_anim = random.choice(self._look_anims)
                self._enter_temp("glance")
            if self.state == "idle" and now - self.last_event_ts > IDLE_TO_SLEEP:
                self.state = "sleep"

        # 3) working 常驻气泡：点名在跑的任务，每帧刷新（通知气泡存活期间让位）
        if self.state == "working" and not (self.bubble_text and self.bubble_is_notify):
            names = "、".join(self._display_name(s) for s in self.sessions.values()
                             if s["state"] == "working")
            self.bubble_text = working_bubble(self.patrol_on, self.phase, names)
            self.bubble_is_notify = False
            self.bubble_until = now + 2
        elif self.bubble_text and now > self.bubble_until:
            self.bubble_text = None

        # working 巡逻（可用菜单开关）：沿屏底走动，拖拽中/落定期暂停；在所在显示器内折返
        if (self.patrol_on and self.state == "working" and not self._drag
                and now > self.patrol_pause_until):
            wx, wy = self.root.winfo_x(), self.root.winfo_y()
            lo, hi = monitor_span(wx + self.W // 2, wy + self.H // 2)
            x, d = patrol_step(wx, self.patrol_dir, PATROL_SPEED, 0.12,
                               lo, hi - self.W)
            self.patrol_dir = d
            self.root.geometry(f"+{int(x)}+{wy}")

        self.draw()
        self.root.after(120, self.tick)

    def _enter_temp(self, state: str, text: str = None):
        self.state, self.state_ts = state, time.time()
        if text:
            self.say(text)

    def draw(self):
        cv = self.cv
        for i in self._ids + self._bubble_ids:
            cv.delete(i)
        self._ids, self._bubble_ids, self._parts = [], [], {}
        self.draw_sprite()
        self.draw_overlays()
        self.root.title(f"zcode-pet [{self.state}]")

    # ---- codex-pets sprite 渲染 ----

    def draw_sprite(self):
        anim, speed = STATE_ANIM.get(self.state, ("idle", 1.0))
        if self.state == "glance" and self._glance_anim:
            anim = self._glance_anim
        elif self.state == "working" and self.patrol_on:
            side = "running-right" if self.patrol_dir > 0 else "running-left"
            anim = side if side in self._skin_anims else anim  # 关巡逻则原地 running
        if self.pat_until > time.time() and anim != "jumping":
            anim, speed = "jumping", 1.2
        imgs = self._anim_frames(anim)
        if not imgs and anim != "idle":   # 皮肤缺该动画行时回退 idle
            anim = "idle"
            imgs = self._anim_frames(anim)
        if not imgs:
            return
        self.anim_pos = (self.anim_pos + speed) % len(imgs)
        self._ids.append(self.cv.create_image(
            self.DW / 2, self.GROUND, anchor="s", image=imgs[int(self.anim_pos)]))

    def _anim_frames(self, anim: str) -> list:
        if anim in self.frames:
            return self.frames[anim]
        try:
            meta = json.loads((PETS_DIR / self.pid / "meta.json").read_text(encoding="utf-8"))
            _, n = meta["anims"][anim]
            fdir = PETS_DIR / self.pid / "frames"
            imgs = [tk.PhotoImage(file=str(fdir / f"{anim}_{i}.png")) for i in range(n)]
        except (OSError, ValueError, KeyError, tk.TclError):
            imgs = []
        if imgs and self.scale != 1.0:
            zn, zd = zoom_factors(self.scale)
            imgs = [im.zoom(zn).subsample(zd) for im in imgs]
        self.frames[anim] = imgs
        return imgs

    def _apply_dims(self):
        """真实窗口 = 基准(200x250, 192x208 帧 + 头顶气泡) × 缩放。"""
        self.W, self.H = round(200 * self.scale), round(250 * self.scale)
        self.DW, self.GROUND = self.W, self.H - round(14 * self.scale)

    def _set_scale(self, s: float):
        old_w, old_h = self.W, self.H
        self.scale = s
        save_settings(scale=s)
        self.frames = {}                # 缓存的帧是按旧缩放生成的
        self._apply_dims()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        # 右下角锚定，缩放时宠物不会往下钻出屏幕
        self.root.geometry(f"{self.W}x{self.H}+{x + old_w - self.W}+{y + old_h - self.H}")
        self.draw()

    def _delete_pet(self, pid: str):
        if not tk.messagebox.askokcancel("zcode-pet", f"删除形象 {pid}？\n（不可恢复，可重新 --download 下载）"):
            return
        remove_pet(pid)
        if pid == self.pid:
            nxt = bundled_default()
            if nxt:
                self._switch_pet(nxt)
            else:  # 最后一只也删了：没有形象可显示，退出
                self.root.destroy()
                return
        self.draw()

    def _set_patrol(self, on: bool):
        self.patrol_on = on
        save_settings(patrol=on)

    def _set_muted(self, on: bool):
        self.muted = on
        save_settings(muted=on)

    def _switch_pet(self, pid: str):
        self.pid = pid
        self.frames = {}
        self._apply_dims()
        self._refresh_skin_meta()
        self.root.geometry(f"{self.W}x{self.H}")
        set_current_pet(self.pid)

    def draw_overlays(self):
        """爱心/气泡覆盖层。"""
        cv, p, G = self.cv, self.phase, self.GROUND
        patted = self.pat_until > time.time()

        def add(fn, *a, **kw):
            i = fn(*a, **kw)
            self._ids.append(i)
            return i

        if patted:
            t = 1 - (self.pat_until - time.time()) / 1.5
            add(cv.create_text, self.DW / 2 - t * 10, G - 80 - t * 28, text="❤",
                font=("Segoe UI Emoji", 11))

        if self.bubble_text:
            self._draw_bubble(self.bubble_text)

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
            fill="#FFFFFF", outline="#7A4E22", width=1.5, smooth=True)
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
            self.last_event_ts = time.time()  # 醒后别下一拍又立刻睡回去
            if self.state == "sleep":
                self.state, self.state_ts = "idle", time.time()
            if random.random() < 0.6:
                self.say(random.choice(PAT_WORDS), 2.0)
        elif self._drag:  # 拖过：落定几秒再继续巡逻，并记住新位置
            self.patrol_pause_until = time.time() + PATROL_SETTLE
            self._save()
        self._drag = None

    def _popup(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="测试「任务完成」提醒", command=lambda: self.on_event("Stop"))
        m.add_command(label="测试「需要确认」提醒", command=lambda: self.on_event("PermissionRequest"))
        m.add_separator()
        var = tk.BooleanVar(value=self.muted)
        m.add_checkbutton(label="静音", variable=var,
                          command=lambda: self._set_muted(var.get()))
        pv = tk.BooleanVar(value=self.patrol_on)
        m.add_checkbutton(label="巡逻", variable=pv,
                          command=lambda: self._set_patrol(pv.get()))
        # 换形象：已下载的 codex-pets 皮肤
        pets_menu = tk.Menu(m, tearoff=0)
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
        # 删除形象（确认后删除，当前形象被删则切默认）
        del_menu = tk.Menu(m, tearoff=0)
        for pdir in sorted(PETS_DIR.glob("*/meta.json")):
            pid = pdir.parent.name
            del_menu.add_command(label=f"🗑 {pid}",
                                 command=lambda pid=pid: self._delete_pet(pid))
        m.add_cascade(label="删除形象", menu=del_menu)
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
    ap.add_argument("--pet", metavar="ID", help="切换当前形象")
    ap.add_argument("--remove", metavar="ID", help="删除已下载的形象（删当前形象则切默认）")
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
    elif a.remove:
        pid = pet_id_from(a.remove)
        assert (PETS_DIR / pid / "meta.json").exists(), f"不存在 {pid}"
        remove_pet(pid)
        cur = current_pet() or bundled_default()
        print(f"已删除 {pid}，当前形象: {cur or '无'}")
    elif a.pet:
        pid = pet_id_from(a.pet)
        assert (PETS_DIR / pid / "meta.json").exists(), f"未下载 {pid}，先 --download {pid}"
        set_current_pet(pid)
        print(f"当前形象: {pid}")
    else:
        if not (current_pet() or bundled_default()):
            r = tk.Tk()
            r.withdraw()
            tk.messagebox.showinfo(
                "zcode-pet",
                "没有可用的宠物形象。\n\n先下载一只（命令行运行）:\n\n"
                "python pet.py --list\npython pet.py --download non0")
            return
        Pet().run()


if __name__ == "__main__":
    main()
