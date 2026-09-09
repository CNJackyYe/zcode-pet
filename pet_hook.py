"""zcode hook -> 桌宠事件桥。zcode 每次触发 hook 事件时调用，
往 ~/.zcode/pet/events.jsonl 追加一行 JSON，桌宠轮询该文件。

用法: python pet_hook.py --event Stop [--sid <会话id>]
sid 标识"是哪个任务"（zcode 注入 ${CLAUDE_SESSION_ID} 模板/环境变量），
name 取会话所在项目目录名，桌宠气泡据此区分并行的多个任务。
"""
import argparse
import json
import os
import time
from pathlib import Path

EVENTS_FILE = Path.home() / ".zcode" / "pet" / "events.jsonl"


def session_id(arg_sid: str) -> str:
    """--sid 模板参数优先（安装时写入 ${CLAUDE_SESSION_ID}），缺省回退环境变量；
    模板未被展开（旧版 zcode 原样透传 "${...}"）视为拿不到。"""
    if arg_sid and not arg_sid.startswith("${"):
        return arg_sid
    return (os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("ZCODE_SESSION_ID") or "")


def task_name() -> str:
    """项目目录名：多任务并行时区分"哪个任务在干活"。"""
    root = (os.environ.get("ZCODE_PROJECT_DIR")
            or os.environ.get("CLAUDE_PROJECT_DIR")
            or os.getcwd())
    return Path(root).name or "任务"


def append_event(path: Path, event: str, ts: float, sid: str = "", name: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"event": event, "ts": ts, "sid": sid, "name": name},
                           ensure_ascii=False) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--sid", default="")
    a = ap.parse_args()
    append_event(EVENTS_FILE, a.event, time.time(), session_id(a.sid), task_name())
