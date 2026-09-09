"""assert 自检：python test_pet.py"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import pet  # noqa: E402
import pet_hook  # noqa: E402

PY = sys.executable


def test_read_new_events():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "ev.jsonl"
        evs, off = pet.read_new_events(f, 0)
        assert evs == [] and off == 0, "不存在的文件应返回空"

        f.write_text('{"event":"Stop","ts":1,"sid":"sa","name":"alpha"}\n'
                     '{"event":"UserPromptSubmit","ts":2}\n', encoding="utf-8")
        evs, off = pet.read_new_events(f, 0)
        assert evs == [("Stop", "sa", "alpha"), ("UserPromptSubmit", "", None)], evs
        assert off == f.stat().st_size

        # 半行不读，等补全后一次读
        with open(f, "a", encoding="utf-8") as fh:
            fh.write('{"event":"Perm')
        evs, off2 = pet.read_new_events(f, off)
        assert evs == [] and off2 == off, "不完整尾行不应产出事件"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write('issionRequest","ts":3}\n垃圾行\n')
        evs, off3 = pet.read_new_events(f, off2)
        assert evs == [("PermissionRequest", "", None)], evs  # 垃圾行被跳过

        # 文件被清空（轮转）：offset 回退后重新读
        f.write_text('{"event":"Stop","ts":9}\n', encoding="utf-8")
        evs, off4 = pet.read_new_events(f, off3)
        assert evs == [("Stop", "", None)] and off4 == f.stat().st_size, "轮转后应从头读"
    print("ok read_new_events: 完整行/半行/垃圾行/轮转/sid透传")


def test_event_map_states():
    for ev, state in pet.EVENT_MAP.items():
        assert state in ("greet", "working", "attention", "oops", "done"), ev
    assert set(pet.EVENT_MAP) <= set(pet.HOOK_EVENTS), "EVENT_MAP 必须都能被 hook 安装覆盖"
    print("ok EVENT_MAP: 事件全部映射到合法状态")


def test_session_states():
    now = 1000.0
    nss = pet.session_next_state
    assert nss("working", now - pet.WORKING_TIMEOUT - 1, now, now) == "idle", "working 超时判死"
    assert nss("working", now - 1, now, now) == "working"
    assert nss("attention", now, now - pet.ATTENTION_TTL - 1, now) == "idle", "审阅超时视为已处理"
    assert nss("oops", now, now - pet.TEMP_STATE_SECONDS - 1, now) == "working", "工具失败闪完继续干"
    assert nss("done", now, now - pet.TEMP_STATE_SECONDS - 1, now) == "idle"
    assert nss("greet", now, now - pet.TEMP_STATE_SECONDS - 1, now) == "idle"
    # 聚合优先级：attention > working > oops > done > greet > idle
    agg = pet.aggregate_state
    assert agg({}) == "idle"
    assert agg({"a": {"state": "working"}, "b": {"state": "done"}}) == "working", "还有人干就 working"
    assert agg({"a": {"state": "attention"}, "b": {"state": "working"}}) == "attention"
    assert agg({"a": {"state": "done"}}) == "done"
    print("ok session_states: 单会话流转 + 多会话聚合优先级")


def test_hook_subprocess(tmp_home):
    """真实跑一遍 pet_hook.py（HOME 指向临时目录），验证落盘内容。"""
    env = {**os.environ, "USERPROFILE": str(tmp_home)}
    d = tempfile.mkdtemp()
    r = subprocess.run([PY, str(HERE / "pet_hook.py"), "--event", "Stop", "--sid", "sess1"],
                       capture_output=True, env=env, cwd=d, timeout=10)
    assert r.returncode == 0, r.stderr
    # 无 --sid 时回退环境变量
    r2 = subprocess.run([PY, str(HERE / "pet_hook.py"), "--event", "Stop"],
                        capture_output=True, env={**env, "CLAUDE_SESSION_ID": "fromenv"},
                        cwd=d, timeout=10)
    assert r2.returncode == 0, r2.stderr
    f = tmp_home / ".zcode" / "pet" / "events.jsonl"
    lines = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["event"] == "Stop" and lines[0]["ts"] > 0
    assert lines[0]["sid"] == "sess1"
    assert lines[0]["name"] == Path(d).name, "任务名应取项目目录名"
    assert lines[1]["sid"] == "fromenv", "无 --sid 应回退环境变量"
    print("ok pet_hook: 子进程写入 sid/name（--sid 与环境变量双通道）")


def test_install_uninstall(tmp_home):
    """对临时 config.json 做安装/卸载合并，不动真实配置。"""
    cfg_path = tmp_home / ".zcode" / "cli" / "config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps({"plugins": {"x": 1}}), encoding="utf-8")

    cfg = pet.merge_hooks(cfg_path, PY, str(HERE / "pet_hook.py"))
    assert cfg["plugins"] == {"x": 1}, "原配置键必须保留"
    h = cfg["hooks"]
    assert h["enabled"] is True
    assert set(h["events"]) == set(pet.HOOK_EVENTS)
    for grp in h["events"].values():
        hook = grp[0]["hooks"][0]
        assert hook["type"] == "process" and hook["command"] == PY
        assert hook["args"][0].endswith("pet_hook.py")

    # 幂等：重复安装不叠加
    cfg2 = pet.merge_hooks(cfg_path, PY, str(HERE / "pet_hook.py"))
    assert len(cfg2["hooks"]["events"]["Stop"]) == 1

    # 未动过手的自定义 hooks 不应被卸载
    cfg_path.write_text(json.dumps({"hooks": {"enabled": True, "events": {
        "Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}}), encoding="utf-8")
    cfg3 = pet.remove_hooks(cfg_path)
    assert "hooks" in cfg3, "别人的 hooks 不该被卸载"

    # 指向 pet_hook 的 hooks 可以卸载
    pet.merge_hooks(cfg_path, PY, str(HERE / "pet_hook.py"))
    cfg4 = pet.remove_hooks(cfg_path)
    assert "hooks" not in cfg4
    print("ok install/uninstall: 合并保留原键/幂等/不误删")


def test_interactions():
    """实例化真实 Pet，模拟鼠标事件序列验证 拖拽/单击撸猫/睡觉唤醒。"""
    import pet as _pet
    try:
        p = _pet.Pet()
    except Exception as e:  # 无显示环境时跳过
        print(f"skip interactions: {e}")
        return

    class E:  # 模拟 tkinter 事件对象（x/y=窗口客户区坐标，x_root/y_root=屏幕坐标）
        def __init__(self, x, y, x_root, y_root):
            self.x, self.y, self.x_root, self.y_root = x, y, x_root, y_root

    p.root.update_idletasks()
    gx0, gy0 = p.root.winfo_x(), p.root.winfo_y()

    # 单击（屏幕位移低于阈值）→ 撸猫反应
    p.state, p.state_ts = "sleep", time.time()
    p._press(E(88, 150, 1000, 800)); p._drag_move(E(90, 152, 1002, 802)); p._release(E(90, 152, 1002, 802))
    assert p.state == "idle", "单击应把睡猫唤醒"
    assert p.pat_until > time.time(), "单击应有撸猫反应"

    # 拖拽（屏幕位移 +62,+50）→ 窗口位移，且不算撸猫
    p.pat_until = 0
    p._press(E(88, 150, 1000, 800))
    p._drag_move(E(138, 190, 1050, 830)); p._drag_move(E(150, 200, 1062, 850))
    p._release(E(150, 200, 1062, 850))
    p.root.update()  # geometry() 异步生效，等窗口管理器应用
    assert p.root.winfo_x() == gx0 + 62 and p.root.winfo_y() == gy0 + 50
    assert p.pat_until == 0, "拖拽不应触发撸猫"

    # 防抽搐回归：窗口移动后 Windows 补发"假 MOUSEMOVE"（指针屏幕位置没变）
    # → 窗口必须原地不动，否则就是来回抽搐的老 bug
    p._press(E(88, 150, 2000, 900))
    p._drag_move(E(138, 190, 2050, 930))
    p.root.update()
    x_moved = p.root.winfo_x()
    p._drag_move(E(88, 140, 2050, 930))  # 假事件：客户区坐标变了但屏幕坐标没变
    p.root.update()
    assert p.root.winfo_x() == x_moved, "静止指针的假事件不应挪动窗口（抽搐回归）"
    p._release(E(88, 140, 2050, 930))

    # 事件驱动流转：on_event("Stop") → done + 气泡
    p.muted = True
    p.on_event("Stop")
    assert p.state == "done" and p.bubble_text == "✅ 任务完成！"

    p.root.destroy()
    print("ok interactions: 单击撸猫/拖拽移动/睡觉唤醒/on_event 流转")


def test_codex_pet_support():
    # URL / id 解析
    assert pet.pet_id_from("non0") == "non0"
    assert pet.pet_id_from("https://codex-pets.net/#/pets/non0") == "non0"

    # 状态映射的动画必须在 v1 图集定义里（v1 是 v2 子集，向下兼容两种图集）
    v1_ids = {a for a, _, _ in pet.ANIMS_V1}
    for state, (anim, speed) in pet.STATE_ANIM.items():
        assert anim in v1_ids, f"{state} 映射到不存在的动画 {anim}"
        assert 0 < speed <= 2

    # 图集规格自洽：行号/帧数不越界
    assert max(r for _, r, _ in pet.ANIMS_V1) == 8          # 9 行
    assert max(r for _, r, _ in pet.ANIMS_V2) == 10         # 11 行
    assert all(n <= pet.COLS for _, _, n in pet.ANIMS_V2)   # 每行 ≤ 8 帧

    # 已下载宠物（non0）资产完整性：meta 里的每个动画每一帧都存在
    mfile = pet.PETS_DIR / "non0" / "meta.json"
    if not mfile.exists():
        print("skip codex 资产检查: 未下载 non0")
        return
    meta = json.loads(mfile.read_text(encoding="utf-8"))
    assert meta["version"] == 2
    fdir = pet.PETS_DIR / "non0" / "frames"
    for anim, (_, n) in meta["anims"].items():
        for i in range(n):
            assert (fdir / f"{anim}_{i}.png").exists(), f"缺帧 {anim}_{i}"
    print(f"ok codex-pets: 映射/规格自洽/资产完整({sum(n for _, n in meta['anims'].values())} 帧)")


def test_scaling():
    import pet as _pet
    try:
        p = _pet.Pet()
    except Exception as e:
        print(f"skip scaling: {e}")
        return
    try:
        # 档位 -> 整数分数映射（tk zoom/subsample 组合）
        assert _pet.zoom_factors(0.5) == (1, 2)
        assert _pet.zoom_factors(0.75) == (3, 4)
        assert _pet.zoom_factors(1.0) == (1, 1)
        assert _pet.zoom_factors(1.5) == (3, 2)
        assert _pet.zoom_factors(2.0) == (2, 1)

        # 先归一到 100%，与持久化设置解耦（用户可能正用其它档位）
        p._set_scale(1.0)
        p.root.update()
        base_w, base_h = p.W, p.H
        p._set_scale(1.5)
        p.root.update()
        assert p.W == round(base_w * 1.5) and p.H == round(base_h * 1.5)
        assert p.root.winfo_width() == p.W, "窗口应随缩放变大"
        assert _pet.load_settings()["scale"] == 1.5
        if p.pid:  # sprite 模式：帧图真实放大到 192*1.5
            imgs = p._anim_frames("idle")
            assert imgs and imgs[0].width() == 288, imgs[0].width() if imgs else None

        p._set_scale(0.5)
        p.root.update()
        assert p.root.winfo_width() == round(base_w * 0.5)
    finally:
        p._set_scale(1.0)
        p.root.destroy()
    print("ok scaling: 档位分数/窗口尺寸/帧图缩放/持久化")


def test_startup_no_replay(tmp_events):
    """桌宠启动 offset 应跳到文件末尾：不重放历史事件（否则每次启动闪旧提醒）。"""
    import pet as _pet
    saved = _pet.EVENTS_FILE
    _pet.EVENTS_FILE = tmp_events
    try:
        p = _pet.Pet()
        assert p.offset == tmp_events.stat().st_size, "启动应从文件末尾开始"
        p.root.destroy()
    finally:
        _pet.EVENTS_FILE = saved
    print("ok startup: 不重放历史事件")


def test_patrol_step():
    x, d = pet.patrol_step(100, 1, 60, 0.12, 0, 1720)
    assert abs(x - 107.2) < 0.01 and d == 1, "应向右推进 speed*dt"
    x, d = pet.patrol_step(1721, 1, 60, 0.12, 0, 1720)
    assert x == 1720 and d == -1, "超出右界应贴界并折返向左"
    x, d = pet.patrol_step(3, -1, 60, 0.12, 0, 1720)
    assert x == 0 and d == 1, "超出左界应贴界并折返向右"
    # 副屏在主屏左侧（x 为负）：负坐标边界同样成立
    x, d = pet.patrol_step(-1079, -1, 60, 0.12, -1080, -200)
    assert x == -1080 and d == 1, "负坐标副屏左界折返"
    x, d = pet.patrol_step(-201, 1, 60, 0.12, -1080, -200)
    assert x == -200 and d == -1, "负坐标副屏右界折返"
    x, d = pet.patrol_step(500, -1, 60, 0, 0, 1720)
    assert x == 500 and d == -1, "dt=0 不动"
    print("ok patrol_step: 推进/折返/负坐标副屏/静止")


def test_monitor_span():
    # 主屏内取主屏，副屏内取副屏（此机副屏 [-1080,0)）
    lo, hi = pet.monitor_span(960, 500)
    assert lo <= 960 < hi and hi - lo > 0, (lo, hi)
    lo2, hi2 = pet.monitor_span(-500, 500)
    if lo2 != 0 or hi2 != 1920:  # 单屏机器回退主屏，双屏机器应是副屏
        assert lo2 <= -500 < hi2, (lo2, hi2)
    print(f"ok monitor_span: 主屏({lo},{hi}) 副屏查询({lo2},{hi2})")


def test_t001_state_maps():
    assert pet.STATE_ANIM["attention"] == ("review", 1.0), "权限确认应播 review"
    assert "review_wait" in pet.STATE_ANIM and "glance" in pet.STATE_ANIM
    for st in pet.STATE_ANIM:
        assert st in ("idle", "working", "done", "greet", "attention", "oops",
                      "sleep", "review_wait", "glance"), st
    print("ok T001 状态映射: attention=review / review_wait / glance")


def test_multi_session(tmp_pets_v1):
    """多任务并行（用户核心诉求）：A 完成提醒"B 还在继续"，working 气泡点名。"""
    import pet as _pet
    saved = _pet.PETS_DIR
    _pet.PETS_DIR = tmp_pets_v1
    try:
        p = _pet.Pet(pet_id="v1pet")
        p.muted = True
        # A、B 两个任务先后开工
        p.on_event("UserPromptSubmit", "sa", "alpha")
        assert p.state == "working"
        p.tick()
        assert "alpha" in p.bubble_text, f"working 气泡应点名: {p.bubble_text}"
        p.on_event("UserPromptSubmit", "sb", "beta")
        p.tick()
        assert "alpha" in p.bubble_text and "beta" in p.bubble_text
        # A 完成、B 还在跑：提醒点名 + 宠物保持 working（继续巡逻）
        p.on_event("Stop", "sa", "alpha")
        assert p.state == "working", "B 还在继续，不应庆祝收工"
        assert p.bubble_text == "✅ alpha 任务完成！（beta 还在继续）", p.bubble_text
        # 最后一个也完成：全套收工
        p.on_event("Stop", "sb", "beta")
        assert p.state == "done"
        assert p.bubble_text == "✅ beta 任务完成！", p.bubble_text
        # A 等审阅：优先展示（要人操作）
        p.on_event("UserPromptSubmit", "sa", "alpha")
        p.on_event("PermissionRequest", "sa", "alpha")
        assert p.state == "attention" and p.bubble_text == "📋 alpha 等你审阅"
        # 同目录两个会话重名：加 sid 前缀区分
        p.on_event("UserPromptSubmit", "sc", "alpha")
        p.on_event("Stop", "sc", "alpha")
        assert p.bubble_text.startswith("✅ alpha·sc 任务完成"), p.bubble_text
        p.root.destroy()
    finally:
        _pet.PETS_DIR = saved
    print("ok multi_session: A完成B继续/全部收工/审阅优先/重名区分")


def test_t001_runtime(tmp_pets_v1):
    """v1 皮肤无 look 行禁用张望；done 收尾 30s 后触发 review_wait。"""
    import pet as _pet
    saved = _pet.PETS_DIR
    _pet.PETS_DIR = tmp_pets_v1
    try:
        p = _pet.Pet(pet_id="v1pet")            # 只在临时目录找皮肤
        assert p._skin_anims == {"idle"} and p._look_anims == [], "v1 应无 look 动画"
        p.muted = True
        p.on_event("Stop")
        assert p.state == "done"
        # 熟化会话 done 到期 → 下一个 tick 应预约 followup 并回 idle
        p.sessions[""]["state_ts"] = time.time() - _pet.TEMP_STATE_SECONDS - 1
        p.tick()
        assert p.state == "idle" and p.review_followup_at > 0, "done 收尾应预约 review_wait"
        p.review_followup_at = time.time() - 1   # 快进 30 秒
        p.tick()
        assert p.state == "review_wait" and p.bubble_text == "看看我的成果？"
        p.root.destroy()
    finally:
        _pet.PETS_DIR = saved
    print("ok T001 runtime: v1 禁张望 / done→review_wait 调度")


def test_bundled_pets():
    """内置皮肤开箱即用：默认形象存在且帧完整。"""
    d = pet.bundled_default()
    assert d, "仓库应至少内置一只皮肤"
    meta = json.loads((pet.PETS_DIR / d / "meta.json").read_text(encoding="utf-8"))
    fdir = pet.PETS_DIR / d / "frames"
    for anim, (_, n) in meta["anims"].items():
        for i in range(n):
            assert (fdir / f"{anim}_{i}.png").exists(), f"缺帧 {d}/{anim}_{i}"
    total = sum(v[1] for v in meta["anims"].values())
    print(f"ok bundled: 默认形象 {d}（{total} 帧完整）")


def test_patrol_toggle():
    import pet as _pet
    initial = _pet.load_settings()["patrol"]   # 用户可能改过开关，测完恢复
    _pet.save_settings(patrol=True)
    try:
        p = _pet.Pet()
    except Exception as e:
        print(f"skip patrol toggle: {e}")
        return
    try:
        assert p.patrol_on is True, "设置 patrol=True 时应开启巡逻"
        p.root.update()  # 让初始 geometry 落定，否则基准坐标是未生效的旧值
        p.muted = True
        p._set_patrol(False)
        assert _pet.load_settings()["patrol"] is False
        p.on_event("UserPromptSubmit")
        p.tick()
        assert p.bubble_text.startswith("🐾 工作中"), f"关巡逻应显示工作中: {p.bubble_text}"
        x0 = p.root.winfo_x()
        for _ in range(10):
            p.tick(); p.root.update()
        assert p.root.winfo_x() == x0, "关闭巡逻后 working 状态不应移动窗口"
        p._set_patrol(True)
        p.tick()
        assert p.bubble_text.startswith("🐾 巡逻中"), f"开巡逻应显示巡逻中: {p.bubble_text}"
    finally:
        p._set_patrol(initial)
        p.root.destroy()
    print("ok patrol toggle: 开关持久化/关闭后原地不动/气泡文案跟随开关")


def test_settings_persistence():
    """设置统一持久化：往返读写 / 位置恢复 / 越界位置回默认。"""
    import pet as _pet
    f = _pet.PETS_DIR / "settings.json"
    bak = f.read_bytes() if f.exists() else None
    try:
        # 往返
        _pet.save_settings(scale=1.5, patrol=False, muted=True, x=100, y=100)
        s = _pet.load_settings()
        assert s["scale"] == 1.5 and s["patrol"] is False and s["muted"] is True

        # 位置恢复：退出位置 +300+300，下次启动直接落座
        p = _pet.Pet()
        p.root.update()
        p.root.geometry("+300+300")
        p.root.update()
        p._save()
        p.root.destroy()
        p2 = _pet.Pet()
        p2.root.update()
        assert (p2.root.winfo_x(), p2.root.winfo_y()) == (300, 300), "应恢复上次退出位置"

        # 越界位置（换显示器后）→ 回默认右下角
        _pet.save_settings(x=99999, y=99999)
        p2.root.destroy()
        p3 = _pet.Pet()
        p3.root.update()
        assert p3.root.winfo_x() == p3.root.winfo_screenwidth() - p3.W - 40, "越界应回默认位置"
        p3.root.destroy()
    finally:
        f.parent.mkdir(exist_ok=True)
        if bak is None:
            f.unlink(missing_ok=True)
        else:
            f.write_bytes(bak)
    print("ok settings: 往返/位置恢复/越界回默认")


def test_remove_pet():
    """删除皮肤：目录移除；删当前形象自动切默认；删到最后一只清空选择。"""
    import pet as _pet
    saved = _pet.PETS_DIR
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for pid in ("aaa", "bbb"):
            pdir = root / pid
            (pdir / "frames").mkdir(parents=True)
            (pdir / "meta.json").write_text(
                json.dumps({"id": pid, "version": 1, "anims": {"idle": [0, 1]}}),
                encoding="utf-8")
        _pet.PETS_DIR = root
        try:
            _pet.set_current_pet("aaa")
            _pet.remove_pet("bbb")                       # 删非当前
            assert not (root / "bbb").exists() and _pet.current_pet() == "aaa"
            _pet.remove_pet("aaa")                       # 删当前 → 无默认可切
            assert not (root / "aaa").exists() and _pet.current_pet() is None
            # 再放两只：删当前应切到剩下那只
            for pid in ("ccc", "ddd"):
                pdir = root / pid
                (pdir / "frames").mkdir(parents=True)
                (pdir / "meta.json").write_text(
                    json.dumps({"id": pid, "version": 1, "anims": {"idle": [0, 1]}}),
                    encoding="utf-8")
            _pet.set_current_pet("ccc")
            _pet.remove_pet("ccc")
            assert _pet.current_pet() == "ddd", "删当前形象应切到剩下的"
        finally:
            _pet.PETS_DIR = saved
    print("ok remove_pet: 删非当前/删到空/删当前切默认")


if __name__ == "__main__":
    import time
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        test_read_new_events()
        test_event_map_states()
        test_session_states()
        test_hook_subprocess(home)
        test_install_uninstall(home)
    test_codex_pet_support()
    test_bundled_pets()
    test_interactions()
    test_scaling()
    test_patrol_step()
    test_monitor_span()
    test_patrol_toggle()
    test_settings_persistence()
    test_remove_pet()
    test_t001_state_maps()
    with tempfile.TemporaryDirectory() as d:
        import pet as _pet
        evf = Path(d) / "events.jsonl"
        evf.write_text('{"event":"Stop","ts":1}\n{"event":"Stop","ts":2}\n', encoding="utf-8")
        test_startup_no_replay(evf)
        v1dir = Path(d) / "pets"
        (v1dir / "v1pet").mkdir(parents=True)
        (v1dir / "v1pet" / "meta.json").write_text(
            json.dumps({"id": "v1pet", "version": 1, "anims": {"idle": [0, 1]}}),
            encoding="utf-8")
        test_t001_runtime(v1dir)
        test_multi_session(v1dir)
    print("ALL TESTS PASSED")
