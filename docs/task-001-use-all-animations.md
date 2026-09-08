# 任务单 T001：把 codex-pets 全部 11 种动作用起来

> 类型：功能增强 ｜ 优先级：P1 ｜ 状态：**已完成（2026-09-08）**
> 创建：2026-09-08 ｜ 提交人：用户

## 背景

codex-pets 皮肤自带 11 种动画（v1 前 9 行 + v2 新增 2 行 look），v1.0 状态机只映射了
6 种，浪费 5 种。另：按用户要求，内置矢量橘猫已移除，形象只走 codex-pets.net。

## 设计方案与落实情况

### 1. 工作巡逻（run-right / run-left）✅

- `working` 状态以 60px/s 沿屏幕底部来回走动（`patrol_step` 纯函数：推进+边缘折返）
- 动画按移动方向取 running-right / running-left（皮肤缺行回退 running）
- 拖拽暂停、放下后定 3 秒（`PATROL_SETTLE`）再继续；仅主显示器

### 2. 等待审阅（review）✅

- `PermissionRequest` → review 动画 + 气泡「📋 等你审阅」
- `done` 收尾 30 秒后仍 idle → 一次性 review_wait「看看我的成果？」
  （ponytail 天花板：不做用户无操作检测，固定延迟代替）

### 3. 眼神张望（look-right-side / look-left-side）✅

- v2 皮肤 idle 且安静时每 20~60s 随机张望 2.5s；v1 缺行自动禁用
- 进阶"鼠标跟随"未做（YAGNI，后续有需求再说）

## 验收标准（全部达成）

- [x] working 状态宠物会走动，动画方向与移动方向一致，到屏幕边缘折返
      （实测窗口 3s 位移 168px ≈ 56px/s；方向动画帧断言 running-right/left）
- [x] PermissionRequest 播放 review 动画 + 新气泡文案
      （进程内画布断言：气泡文本「📋 等你审阅」、帧缓存含 review）
- [x] idle 随机张望在 v2 皮肤生效；v1 皮肤无报错静默跳过
      （glance 触发断言 look-right-side；v1 临时夹具 _look_anims == []）
- [x] 拖拽期间巡逻暂停，不与防抽搐锚定逻辑冲突（复用既有交互测试）
- [x] `test_pet.py` 新增 4 组断言，12 组全部通过

## 技术备注（修正）

- ~~方向翻转需要 Pillow 预生成镜像帧~~ **不需要**：run-right/run-left、
  look-right/look-left 在 v1/v2 图集中均为原生成对动画行
- 巡逻窗口移动用 geometry 直接设屏幕坐标，无相对坐标抽搐问题
