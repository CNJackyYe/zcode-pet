"""zcode hook -> 桌宠事件桥。zcode 每次触发 hook 事件时调用，
往 ~/.zcode/pet/events.jsonl 追加一行 JSON，桌宠轮询该文件。

用法: python pet_hook.py --event Stop
"""
import argparse
import json
import time
from pathlib import Path

EVENTS_FILE = Path.home() / ".zcode" / "pet" / "events.jsonl"


def append_event(path: Path, event: str, ts: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"event": event, "ts": ts}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    a = ap.parse_args()
    append_event(EVENTS_FILE, a.event, time.time())
