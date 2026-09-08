# 任务单 T001：把 codex-pets 全部 11 种动作用起来

> 类型：功能增强 ｜ 优先级：P1 ｜ 状态：待排期
> 创建：2026-09-08 ｜ 提交人：用户 ｜ 来源：v1.0 交付后反馈

## 背景

codex-pets 皮肤自带 11 种动画（v1 前 9 行 + v2 新增 2 行 look），目前 zcode-pet
状态机只映射了 6 种，浪费了 5 种：

| 动画 | 图集行 | 现状 |
|---|---|---|
| idle / running / jumping / waving / failed / waiting | 0/7/4/3/5/6 | ✅ 已用 |
| **run-right / run-left** | 1/2 | ❌ 闲置 |
| **review** | 8 | ❌ 闲置 |
| **look-right-side / look-left-side** | 9/10 | ❌ 闲置（v2 专属，各 8 帧顺时针 16 朝向） |

## 设计方案

### 1. 工作巡逻（run-right / run-left）——桌宠经典行为

- `working` 状态下不再原地奔跑：宠物以 run-right / run-left 在屏幕底部来回走动
- 到屏幕边缘自动翻转方向
- 用户拖拽期间暂停巡逻，放下后从新位置继续
- 第一期只支持主显示器（多屏后续再说）

### 2. 等待审阅（review）——语义完美契合

- `PermissionRequest`（zcode 等你确认权限）从 waving 改为 **review** 动画，
  气泡文案改"📋 等你审阅"
- `done` 提醒 6 秒后若用户仍无操作，追加一次 review 播放（"看看我的成果？"）

### 3. 眼神跟随（look-right-side / look-left-side）

- idle 状态每 20~60 秒随机张望 2~3 秒（look-left 或 look-right），增加生命感
- 进阶（可选）：v2 look 行每帧是一个朝向，按鼠标相对宠物的方位角选帧，
  实现"宠物盯着你的鼠标看"
- v1 皮肤没有 look 行 → 自动跳过该行为（回退策略：不报错、不播放）

## 验收标准

- [ ] working 状态宠物会走动，动画方向与移动方向一致，到屏幕边缘折返
- [ ] PermissionRequest 播放 review 动画 + 新气泡文案
- [ ] idle 随机张望在 v2 皮肤生效；v1 皮肤无报错静默跳过
- [ ] 拖拽期间巡逻暂停，不与防抽搐锚定逻辑冲突
- [ ] `test_pet.py` 新增对应断言，全部通过

## 技术注意点

- 巡逻 = 定时改窗口 geometry，必须复用防抽搐的**屏幕坐标锚定**经验（见
  `pet.py _drag_move` 的 ponytail 注释），避免重蹈相对坐标覆辙
- 方向翻转需要水平镜像帧：tk 无 flip API，建议**下载期用 Pillow 预生成
  `_flip` 镜像帧**（运行时仍零依赖）
- review 与 waiting 语义区分主要靠气泡文案，动画本身相近
