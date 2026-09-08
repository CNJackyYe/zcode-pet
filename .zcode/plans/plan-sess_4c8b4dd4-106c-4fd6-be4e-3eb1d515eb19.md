# T001 开发计划：启用全部 11 种动画

## 设计结论（含对任务单的一处修正）

**镜像帧不需要**：run-right/run-left、look-right/look-left 在 v1/v2 图集中均为原生成对动画行，无需 Pillow 预生成 `_flip` 镜像。实施时同步修正任务单该条注意点。

**鼠标跟随（进阶项）本期不做**：任务单标注"可选"，YAGNI；look 行先用于随机张望。

## 改动范围

全部落在 `pet.py`（+约 90 行）、`test_pet.py`、`README.md`、`docs/task-001-*.md`。

### 1. 工作巡逻（run-right / run-left）

- 纯函数 `patrol_step(x, dir, speed, dt, screen_w, win_w) -> (x, dir)`：按速度推进、越界折返（便于单测）
- `Pet` 增加 `patrol_dir`、`patrol_pause_until`；`tick()` 中仅 `state=="working"` 时调用：
  - 窗口 x 每帧（120ms）移动 ~7px（约 60px/s），用 `geometry("+x+y")` 直接设位置（窗口移动天然是屏幕坐标，无抽搐问题）
  - 动画选择：向右 `running-right`、向左 `running-left`（`STATE_ANIM["working"]` 改为按方向动态取）
  - 拖拽期间（`self._drag` 非空）暂停；放下后 3 秒定住再继续（宠物"落定"）
  - 仅主显示器；Stop 后停在原地进入 done
- 内置橘猫（vector 模式）：无左右动画差异，照常巡逻移动即可（身体不翻转）

### 2. 等待审阅（review）

- `STATE_ANIM["attention"]`：waving → **review**；`EVENT_MAP` 气泡文案改「📋 等你审阅」
- done 小剧场收尾追加：done 结束时记 `review_followup_at = now + 30`；tick 中若 state 已回 idle 且未播放过，进入临时状态 `review_wait`（review 动画 + 气泡「看看我的成果？」，6 秒回 idle）
  - ponytail 天花板注释：不做"用户无操作"检测（stdlib 无全局输入监听），30 秒固定延迟代替

### 3. 眼神张望（look-right-side / look-left-side，v2 皮肤专属）

- init 时探测 meta 里 look 动画是否存在（v1 缺行 → 全程跳过、零报错）
- idle 且无气泡时：每 20~60s 随机进入临时状态 `glance`（2.5s），`self._glance_anim` 随机取 look 左/右
- `draw_sprite` 对 `glance` 状态取 `_glance_anim` 覆盖

### 4. 测试与验证

- 单测新增：
  - `patrol_step`：推进、右边界折返、左边界折返、dt=0 不动
  - attention 映射 = review；`review_wait`/`glance` 状态映射存在
  - v1 meta（无 look 行）时 glance 探测为不可用
  - done→30s→review_wait 调度逻辑（构造时间断言）
- E2E：注入 UserPromptSubmit，隔 2 秒截两张图对比宠物位置移动；注入 PermissionRequest 截 review 动画
- 全量 `python test_pet.py` 绿后：更新 README 状态表、勾掉任务单验收项

### 5. 交付

- 一次提交推送 gitee（main + master），桌宠进程重启到新版本
