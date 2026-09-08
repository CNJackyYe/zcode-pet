# zcode-pet 🐱

**给你的 zcode 养一只桌面宠物。** 它趴在屏幕右下角（形象来自 [codex-pets.net](https://codex-pets.net) 社区像素宠物）：zcode 接到任务它就沿屏幕底部巡逻干活，任务完成跳起来提醒，等你审阅时举牌呼叫，闲时好奇张望、打瞌睡——你可以撸它、拖它、换皮肤、调大小。

- **zcode 任务感知**：通过 zcode hooks 实时联动（多会话事件汇到一只宠物身上）
- **11 种动画全用上**：idle / 巡逻(左右奔跑) / 挥手 / 跳跃 / 失败 / 等待 / 奔跑 / 审阅 / 左右张望
- **codex-pets.net 社区皮肤**：`--list` 浏览、`--download` 一条命令换形象
- **零依赖运行**：纯 Python 标准库（tkinter + winsound），下载皮肤时才需要 Pillow
- 交互：拖拽移动 / 单击撸宠 / 右键菜单（测试提醒、静音、巡逻开关、换形象、大小 50%~200%、退出）

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11（透明窗口用了 Windows 特性） |
| Python | 3.10+，自带 tkinter（官方安装包默认勾选） |
| 可选 | [Pillow](https://pypi.org/project/Pillow/)——仅 `--download` 下载皮肤时用来切图集 |

## 安装部署（2 步 + 可选）

```bat
git clone https://gitee.com/dontmove/zcode-pet.git
cd zcode-pet

:: 1. 安装 zcode hooks（写入 ~/.zcode/cli/config.json，自动保留原有配置）
python pet.py --install

:: 2. 启动桌宠（仓库已内置 3 只皮肤，开箱即用，默认 xm-cat 晓冒）
start_pet.bat        :: 或: pythonw pet.py

:: 可选：从 codex-pets.net 下载更多皮肤
python pet.py --list
python pet.py --download non0
```

**内置皮肤**（`pets/` 随仓库分发，右键菜单随时切换）：

| id | 名字 | 图集 | 张望 |
|---|---|---|---|
| xm-cat | 晓冒 | v1（57 帧） | 不支持（v1 无 look 行） |
| clack-astra | Clack Astra | v2（74 帧） | ✓ |
| kanroji-mitsuri | mitsuri | v2（74 帧） | ✓ |

完成。**新开一个 zcode 会话**随便发句话试试：任务跑起来宠物开始巡逻，回复结束它跳起来撒花+响铃。

> 说明：hooks 在 zcode **会话启动时**加载，安装前就开着的旧会话不会触发。
> 需要代理时：`python pet.py --download non0 --proxy http://127.0.0.1:7897`，或先设 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量。

**开机自养**：Win+R 输入 `shell:startup` 回车，把 `start_pet.bat` 的快捷方式丢进去。

## 工作原理

```
zcode 会话 ──hook事件──> pet_hook.py ──追加一行JSON──> ~/.zcode/pet/events.jsonl
                                                            │ 桌宠每500ms增量轮询
                                                            ▼
              pet.py（tkinter 透明置顶窗口 + 状态机 + 逐帧动画）
```

**事件 → 状态 → 动画**（codex-pets 图集 11 行动画全启用）：

| zcode 事件 | 宠物表现 | 动画行 |
|---|---|---|
| SessionStart | 上线打招呼 | waving |
| UserPromptSubmit | "🐾 巡逻中…"，沿屏幕底部来回走动 | running-right / running-left |
| **Stop（任务完成）** | **跳跃+"✅ 任务完成！"+提示音** | jumping |
| （完成后 30s 没动静） | "看看我的成果？" | review |
| PermissionRequest | "📋 等你审阅"+提示音 | review |
| PostToolUseFailure | "💢 有个工具出错了" | failed |
| 空闲随机 | 好奇张望 2.5 秒（v2 皮肤） | look-right / look-left |
| 空闲 5 分钟 | 打瞌睡 | waiting 慢放 |
| working 超 15 分钟无事件 | 视为中断回待机 | idle |

其他细节：

- 巡逻速度 60px/s，在**宠物所在显示器**的边界内折返（多显示器：拖到哪块屏就在哪块屏巡逻）；拖拽期间暂停，放下后定 3 秒再继续
- 皮肤格式：codex-pets 官方图集 v1=1536×1872(9行) / v2=1536×2288(11行)，每帧 192×208、
  8 列；下载时 Pillow 按官方动画行定义切成逐帧 PNG 存到 `pets/<id>/frames/`
- 缩放用 `zoom(n).subsample(d)` 分数组合最近邻采样，像素画不糊
- v1 皮肤没有 look 行（张望自动禁用）、缺动画行时回退 idle，均零报错

## 命令与交互

```bat
python pet.py --install            :: 安装 hooks
python pet.py --uninstall          :: 移除 hooks（自动保留其它配置）
python pet.py --list [关键词]       :: 浏览 codex-pets 社区宠物
python pet.py --download <id|URL>  :: 下载皮肤并设为当前形象
python pet.py --pet <id>           :: 切换已下载形象
python pet.py                      :: 启动桌宠
python test_pet.py                 :: 自检（12 组断言）
```

- **拖拽**移动；**单击**撸宠（爱心+说话+跳跃）；**右键**菜单全功能
- 大小档位 50%/75%/100%/150%/200% 持久保存（`pets/size.txt`）

## 常见问题

- **没看到提醒？** hooks 只对安装后**新开**的 zcode 会话生效；确认 `pythonw.exe` 进程在跑；
  事件落盘可查 `~/.zcode/pet/events.jsonl`。
- **启动弹"还没有宠物形象"？** 先 `python pet.py --list` 挑一只再 `--download <id>`。
- **下载 403/超时？** 站点在 Cloudflare 后面，程序已带浏览器 UA；仍不通就配代理（见上）。
- **装 hooks 用的 Python 别卸载**：hooks 记录的是安装时的 python 绝对路径，换了解释器需重跑 `--install`。
- **宠物闪退？** 看 `pet_error.log`；八成是 Python 环境变了。

## 卸载

```bat
python pet.py --uninstall    :: 移除 zcode hooks
```
右键宠物 → 退出，然后删掉整个目录即可。

## 致谢

- 桌宠实现参考社区通行结构：[BITNP/bitnp-desktop-pet](https://github.com/BITNP/bitnp-desktop-pet)、
  [duzexu/desktop-pet](https://github.com/duzexu/desktop-pet)、[shimeji 系](https://github.com/topics/shimeji)
- 皮肤来自 [codex-pets.net](https://codex-pets.net)（[codex-pet-share](https://github.com/portons/codex-pet-share)，MIT），
  版权归各上传者所有，请勿商用

## 许可

MIT
