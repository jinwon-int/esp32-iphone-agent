<div align="center">

# ESP32 iPhone Agent

**让 AI 操作一部真实的 iPhone：只要一块约 40 多元的 ESP32，不需要开发者账号、不需要 Xcode、不用越狱。**

[English](README.md) · 简体中文

演示视频和更新：[X @iamhenryyuan](https://x.com/iamhenryyuan) · [小红书 袁老板讲黑客松（Henry）](https://www.xiaohongshu.com/user/profile/5eb444030000000001000194) · [YouTube 演示](https://www.youtube.com/watch?v=Ly4im-P4jYg)

[![CI](https://github.com/onehumanbeing/esp32-iphone-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/onehumanbeing/esp32-iphone-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Board](https://img.shields.io/badge/board-ESP32--S3-red)

</div>

<p align="center"><img src="docs/images/demo.png" alt="示例 1：在白板里画完笑脸后的网页界面，包括实时画面、实时日志、计划和每一步的截图" width="100%"></p>

**示例 1：在画布上画画。** *"打开白板画一个笑脸"*：Agent 搜索并打开白板 App，用连续的笔画画出笑脸。每一步的截图都会留在截图墙上，上面画出了点按或笔画的位置。

<p align="center"><img src="docs/images/example-alarm.png" alt="示例 2：插着 XIAO ESP32S3 的 iPhone 正在设置每天重复的闹钟，旁边是网页界面" width="100%"></p>

**示例 2：设置闹钟。** *"设置早上 7:00 的闹钟，每天重复，然后保存"*：Agent 打开时钟，在时间滚轮里直接输入 0700，在「重复」里一次连续勾选多天，然后保存。左边是插着 ESP32 的真实 iPhone，右边是网页界面。

Agent 通过 AirPlay 屏幕镜像"看"手机，通过一块插在手机 USB 口上的 ESP32-S3 小板子"摸"手机：板子对手机来说就是一个
USB 鼠标加键盘。大脑可以是任何兼容 OpenAI 接口的视觉模型，云端 API 或自己部署的模型都行。

## 为什么做这个

大多数 iPhone 自动化方案通过 XCTest / WebDriverAgent 控制手机，需要 Mac、Xcode、一个签过名的辅助 App，还要 Apple
开发者账号（免费账号的签名 7 天就过期）。本项目走的是人用手机的路径：看屏幕、动指针，所以手机上什么都不用装。

|                          | ESP32 iPhone Agent     | XCTest / WebDriverAgent 类工具 |
|--------------------------|------------------------|--------------------------------|
| Apple 开发者账号         | **不需要**             | 需要（给测试程序签名）         |
| Xcode                    | **不需要**             | 需要                           |
| 电脑                     | Mac **或 Linux**       | Mac                            |
| 手机上安装 App           | **不需要**             | 需要装测试程序                 |
| 越狱                     | 不需要                 | 不需要                         |
| 额外硬件                 | ESP32-S3 开发板（约 40 多元） | USB 线                    |
| 任意 App（包括游戏）     | 可以（只看像素）       | 大多可以（取决于无障碍控件树） |

## 工作原理

- **眼：AirPlay 镜像。** 手机把屏幕镜像到电脑上运行的 [UxPlay](https://github.com/FDH2/UxPlay)，GStreamer 把视频流
  转成一帧帧 JPEG，供 Agent 和网页使用。
- **手：USB HID 桥。** 插在手机上的 ESP32-S3 被识别为标准的 USB 鼠标和键盘。打开 iOS 的辅助触控（AssistiveTouch）后，
  鼠标会控制屏幕上的一个指针：点击就是点按，拖动就是滑动。板子通过 Wi-Fi 连接电脑，只执行简单的行命令
  （`move`、`click`、`type` 等），所有逻辑都在电脑上。
- **脑：规划模型 + 执行模型。** 规划模型根据目标和当前屏幕写一个简短的计划；执行模型每次看最新截图，选出下一个动作：
  点按、滑动、画一笔、输入、按键、回主屏幕或结束。两个角色可以用同一个模型。
- **闭环校验。** 鼠标是相对移动的，所以服务端会记录指针位置。每次点按前，它会轻推一下指针，在镜像画面里找到它的真实位置，
  偏了就先纠正，同时修正校准。每个动作之后检查屏幕是否真的变化了，并告诉模型。点空了会被发现并重试，而不是将错就错。
  同一个动作重复多次会触发重新规划；断线或模型回复出错都能自动恢复。

## 硬件

| 部件 | 说明 |
|---|---|
| **ESP32-S3 开发板** | 实测使用 [Seeed Studio XIAO ESP32S3](https://wiki.seeedstudio.com/cn/xiao_esp32s3_getting_started/) 普通版、未焊排针（不是带摄像头的 Sense 版，也不是 Plus 版）。国内在淘宝搜“XIAO ESP32S3”就能买到，约 40 多元。任何 USB-C 口直连 S3 原生 USB 的 ESP32-S3 板子都可以。 |
| **iPhone** | USB-C 机型直接插；Lightning 机型用 Lightning 转 USB 相机转换器。板子由手机供电。 |
| **电脑** | macOS 或 Linux，和手机在同一个局域网；运行 UxPlay 和服务端。 |
| **视觉模型** | 任何支持图片输入、兼容 OpenAI 接口的对话模型。本地部署是可选的。 |

<p align="center"><img src="docs/images/setup.png" alt="插在 iPhone 上的 XIAO ESP32S3，旁边是网页界面" width="100%"></p>

如果希望插着板子时手机还能充电，用一个支持 PD 充电直通的 USB-C 扩展坞。

## 快速开始

> **让 AI 编程助手帮你装。** 在板子要连接的那台电脑上，把下面这段话发给 Claude Code、Codex 或类似的 Agent：
>
> ```text
> Clone https://github.com/onehumanbeing/esp32-iphone-agent and follow its README to set it up on this machine:
> flash the ESP32-S3 plugged in over USB, install UxPlay and the server, write server/.env for my model API, start
> the server and give me the web UI address. Ask me for the Wi-Fi name/password and the model API key.
> ```

### 1. 刷固件

安装 [arduino-cli](https://arduino.github.io/arduino-cli/) 和 ESP32 core（3.x），然后：

```bash
python3 -c 'import secrets; print(secrets.token_hex(16))'   # 生成板子密钥：同时填进 secrets.h 和 server/.env
cp hardware/hid_bridge/secrets.example.h hardware/hid_bridge/secrets.h
# 编辑 secrets.h：填 2.4 GHz Wi-Fi 名称和密码、运行服务端的电脑的 IP，以及 DEVICE_KEY
hardware/flash.sh            # 可选参数：[串口] [--debug]
```

板子的 flash 容量默认按 8 MB（XIAO ESP32S3）。4 MB 的板子（例如 ESP32-S3-Zero 或大多数 ESP32-S3 Super Mini）请运行
`FLASH_SIZE=4M hardware/flash.sh`（固件本身约 1 MB）。

**实验性：** `ABSOLUTE_POINTER=1 hardware/flash.sh` 把板子编译成绝对坐标指针（类似数位板）而不是相对鼠标；服务端需相应设置
`POINTER_MODE=absolute`。这样服务端直接发送屏幕位置，无需校准和寻找指针。iOS 是否把它对应到整个屏幕仍在真机上测量（#14）；
默认仍是相对鼠标。绝对坐标指针需要 iOS 18 或更新版本，并请保持竖屏（打开竖排方向锁定）：旋转后坐标可能对不上。
比较两种模式：在主屏幕上分别调用 `POST /api/bench/pointer`，它把指针移到九个位置（不点击），
返回每个位置的偏差和移动耗时 p50/p90（Markdown 表格）。

### 2. 安装并启动服务端

```bash
# UxPlay，macOS：
brew install uxplay
# UxPlay，Debian / Ubuntu：
sudo apt install uxplay gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav

cd server
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env         # 填 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY 和 DEVICE_KEY（与 secrets.h 相同）
set -a && . ./.env && set +a && .venv/bin/python -m hidagent
```

服务端启动时会打印 `web UI: http://<this computer>:8890/?token=…` 和 `AirPlay pin: nnnn`。用浏览器打开这个带 token
的地址一次，浏览器会记住 token 30 天。AirPlay 配对码需要 UxPlay 1.68 或更新版本。

### 3. 设置手机（只需一次）

1. **设置 → 辅助功能 → 触控 → 辅助触控**：打开。
2. 把板子插到手机上，屏幕上会出现一个圆形指针，网页显示控制器已连接。
3. **控制中心 → 屏幕镜像 → iPhone Agent**，输入服务端打印的 AirPlay 配对码（只需第一次），网页里出现实时画面。
4. 在网页里打开 **手动操控 → 校准**。服务端会移动指针、在实时画面里找到它，几秒内自动完成校准；
   如果找不到指针，会提示你在画面里精确点一下指针。
5. 如果网页里的 **主屏幕** 回不到主屏幕（iOS 27 不响应指针从底边上滑，`cmd+h` 也无效），先试 `HOME_METHOD=key`：
   板子发送消费类 Home 键（用法 0x0040，Apple 配件设计指南中对应主屏幕按钮）。若你的手机上无效，再改用辅助触控：
   设置 `HOME_METHOD=assistivetouch` 和 `ASSISTIVETOUCH_BUTTON`（悬浮按钮的位置），再二选一：
   - `ASSISTIVETOUCH_MENU_HOME`：菜单里"主屏幕"的位置（在网页里点一下按钮即可看到），或
   - 在手机上设置 **辅助触控 → 自定操作 → 轻点一下 → 主屏幕**，不设菜单位置。
   请在亮色画面（如主屏幕）上校准：暗色画面里很难找到指针。之后不要移动按钮；移动了就更新设置。
6. 建议在 **辅助触控** 中：*闲置时不透明度* 设为 100 %（半透明按钮难以点中和识别）；在 **指针样式** 中把指针调大、
   换成醒目的颜色，并关闭 *自动隐藏*（在暗色画面里更容易找到）。

### 4. 下达任务

输入一个目标（例如"打开设置，进入 Wi-Fi"），点 **开始**；也可以直接点下面的示例。

## 网页界面

- **实时手机画面**：点击 = 点按，拖动 = 滑动，滚轮 = 滚动；也可以手动回主屏幕、返回、输入文字和按键。
- **实时日志**：每一个计划、思考和动作实时显示，带耗时。
- **步骤截图墙**：每一步模型做决定时看到的截图，上面画出点按或滑动的位置，附带模型的理由和思考、执行各用了多久；
  点卡片可以放大。
- **点按**用红色圆圈和扩散的波纹标出，滑动和笔画用黄色轨迹；**动效**开关可以关掉动画。模型标为 `precise`（点之前先确认指针位置）的点按会在日志里注明。
- **在不可撤销的操作前停下** — 付款、购买、发送、删除你自己的内容、修改账户或安全设置：代理做到最后的确认页就停，显示“等你确认”并说明已就绪的内容，最后一步由你来点（借鉴 [iCTRL](https://github.com/OpenMinis/iCTRL) 的代理规则）。
- **紧急停止**（或按 <kbd>Esc</kbd>）：立即停止 Agent 并松开指针。**清空记录**：清空日志和截图墙。
- 界面支持英文和简体中文。

## 配置

写在 `server/.env` 里（参考 [`.env.example`](server/.env.example)）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:8000/v1` | 兼容 OpenAI 的接口地址 |
| `LLM_MODEL` / `LLM_API_KEY` | — | 模型名称和 API Key |
| `LLM_EXTRA_BODY` | `{}` | 合并进每次请求的 JSON（例如关闭模型的思考模式） |
| `LLM_TOKEN_PARAM` | `auto` | 输出长度参数名：`max_tokens`、`max_completion_tokens`（新版 OpenAI 模型），或 `auto` 自动识别 |
| `ACTOR_*` / `PLANNER_*` | — | 单独覆盖执行模型 / 规划模型的任意 `LLM_*` 配置 |
| `AIRPLAY_NAME` | `iPhone Agent` | 手机"屏幕镜像"列表里显示的名字 |
| `DEVICE_KEY` | —（必填） | 板子共享密钥，与 `secrets.h` 相同，至少 16 个字符 |
| `UI_TOKEN` | 自动生成 | 网页访问 token；不设置时自动生成并保存在 `~/.hidagent/ui_token` |
| `AIRPLAY_PIN` | 自动生成 | 手机首次镜像时输入的 4 位配对码，保存在 `~/.hidagent/airplay_pin`；`off` 表示关闭 |
| `SCREEN_SOURCE` | `airplay` | 屏幕来源：`airplay`（同一网络内的镜像）或 `push`（配套 iOS 应用通过 VPN 推送 JPEG 帧，见英文 README "Screen over a VPN"） |
| `PUSH_HOST` / `PUSH_PORT` | — / `8891` | 仅 `push`：应用连接的 VPN 地址（如本机 Tailscale `100.x` 地址）和端口。必填；拒绝 `0.0.0.0` 等通配地址 |
| `PUSH_TOKEN` | 自动生成 | 仅 `push`：应用出示的令牌；未设置时自动生成并保存在 `~/.hidagent/push_token` |
| `HOST` / `PORT` | `0.0.0.0` / `8890` | 网页监听地址和端口（`HOST=127.0.0.1`：仅本机可访问） |
| `DEVICE_HOST` / `DEVICE_PORT` | `0.0.0.0` / `7300` | 板子连接的地址和端口 |
| `HIDAGENT_DATA` | `~/.hidagent` | 校准数据、密钥和调试记录 |
| `HOME_METHOD` | `swipe` | 回到主屏幕的方式：`swipe`（从底边上滑）、`key`（板子的消费类 Home 键）或 `assistivetouch`（点辅助触控按钮），见"设置手机"；用于不响应指针上滑的 iOS 版本 |
| `ASSISTIVETOUCH_BUTTON` / `ASSISTIVETOUCH_MENU_HOME` | — | 仅 `assistivetouch`：按钮和菜单"主屏幕"项的位置，`x,y` 屏幕比例（如 `0.9,0.27`）。若按钮"轻点一下"已设为主屏幕，可不设菜单项 |
| `GATE_MODE` | `off` | `observe`：每个动作都询问 Jev 并记录答案，见"动作闸门" |
| `POINTER_MODE` | `relative` | 板子用 `ABSOLUTE_POINTER=1` 编译时设为 `absolute`（实验性，见"刷固件"）；须与板子一致 |
| `HIDAGENT_DEBUG` | `0` | 调试模式，见下文 |

### 本地模型（可选）

用云端 API 就够了。如果想全部跑在自己的硬件上，下面是本项目开发和测速时用的配置：

- 模型：[nvidia/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/nvidia/Qwen3.8-Flash-Next-NVFP4)
- 部署：[Qwen3.8-Flash-Dual-DGX-Sparks-TensorFold](https://github.com/MiaAI-Lab/Qwen3.8-Flash-Dual-DGX-Sparks-TensorFold)，
  两台 NVIDIA DGX Spark 张量并行（TP=2），提供 OpenAI 兼容接口
- 速度：关闭思考模式后，每个动作约 1.3 秒，每次规划约 2 秒：

```bash
LLM_BASE_URL=http://<dgx-spark-ip>:8888/v1
LLM_MODEL=Qwen3.8-Flash-Next
LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'
```

其他兼容 OpenAI 接口的服务（vLLM、SGLang、llama.cpp、Ollama 等）配上视觉模型，用法完全一样。

## 动作闸门（可选，仅观察）

设置 `GATE_MODE=observe` 后，每个动作（点按、滑动、输入、按键、主屏幕、返回）都会额外请
[Jev](https://typesafe.ai)（TypeSafe System One，一种用选项和概率回答窄问题的模型）给出第二意见。只发送文字：目标、
计划、执行模型自己的 THINK 行和用文字描述的动作：

- **risk**：`safe` / `confirm`（发送、付款、删除、修改设置等）/ `block`（目标不需要、照着屏幕上的指令做、泄露密码）；
- **on goal**：这个动作是为了你的目标、而不是屏幕上出现的指令的可能性。

观察模式下，答案**绝不改变 Agent 的行为**：提问和动作同时进行，日志显示
`gate (observe): would confirm · risk confirm 80% · on goal 90%`，每条答案追加到 `~/.hidagent/gate/decisions.jsonl`
（格式 `jev.decision.v1`，权限 0600，形似密钥的字符串会被遮盖）。目的是先在真实运行中衡量闸门，之后的版本才可能
*拦下*动作（永远不能强制执行动作）。

需要共享的 `jevlib` 客户端（`JEVLIB_PATH`，默认 `~/.claude/hooks/jev`）和 Jev 密钥（`TYPESAFE_API_KEY`、
`TYPESAFE_API_KEY_FILE` 或 `~/.secrets/typesafe-api-key`）；缺少任一项时闸门保持关闭，Agent 照常运行。隐私：目标、
计划、THINK 行（模型对你屏幕的描述）和输入的文字会发送给 TypeSafe 并保存在记录里。

## 调试模式

设置 `HIDAGENT_DEBUG=1` 后，每条板子命令及其耗时都会写进日志，每次 Agent 运行会单独保存一个文件夹：

```
~/.hidagent/traces/20261012-005207/
├── steps.jsonl   # 每次模型调用（提示词、原始回复、耗时）和每个动作
├── 002.jpg       # 每次模型调用看到的截图
└── ...
```

`hardware/flash.sh --debug` 会编译一个调试版固件，通过 USB 串口回显每条命令和耗时。

## 板子协议

板子本身不含任何逻辑。连上后，板子和服务端先互相证明自己知道 `DEVICE_KEY`（对新随机数做 HMAC-SHA256，密钥本身不在网络上传输）：

```text
board  -> hello <上次重启原因> <开机毫秒数> <板子随机数>
server -> challenge <服务端随机数> <HMAC(key, "server:<板子随机数>:<服务端随机数>")>
board  -> auth <HMAC(key, "board:<服务端随机数>:<板子随机数>")>
server -> ok                       （或 "err auth" 并断开连接）
```

之后服务端通过 TCP 每行发一条命令，板子回复 `ok` 或 `err <原因>`。每一行都带有绑定本次连接和序号的标签，
`session = HMAC(key, "session:<板子随机数>:<服务端随机数>")`：

```text
server -> <命令> <HMAC(session, "cmd:<n>:<命令>") 的前 32 位十六进制>
board  -> <回复> <HMAC(session, "reply:<n>:<回复>") 的前 32 位十六进制>      n = 0, 1, 2, …（每次连接重新计数）
```

标签缺失或错误（伪造、重放、乱序，或超过 512 字节）的行，板子不会执行并直接断开；回复标签错误时服务端也会断开。
长文本按每段 200 个字符分段输入。

| 命令 | 作用 |
|---|---|
| `ping` | 健康检查 |
| `home` | 把指针推到左上角（一个已知位置） |
| `move <dx> <dy>` | 相对移动指针，匀速移动，让 iOS 的指针加速度可预测 |
| `at <x> <y>` | 仅绝对坐标版本：把指针放到 x, y（整个屏幕 0-32767）。此时 `home` 回到左上角，`move` 在上次位置上偏移 |
| `press` / `release` / `click` | 左键按下 / 松开 / 点击 |
| `scroll <v> <h>` | 滚轮 |
| `type <text>` | 输入 ASCII 文字 |
| `key <combo>` | 按键，例如 `enter`、`cmd+space`、`ctrl+space`（切换到下一个键盘语言）；`globe` 像修饰键一样按住，例如 `globe+h` |
| `consumer <key>` | 消费类按键：`home`（0x0040，主屏幕按钮）、`search`（0x0221，聚焦搜索）、`globe`（0x029D，地球键）、`keyboard`（0x01AE，显示/隐藏屏幕键盘），或原始用法 `0x0001`-`0x03ff` |

Wi-Fi 断开时，板子会松开所有按键并自动重连。手机锁屏时 USB 会休眠：板子会先尝试唤醒手机，唤醒不了就立刻回复 `err phone usb asleep`，不会卡住等待。万一板子没有响应，看门狗会让它自动重启。

## 目录结构

```
hardware/   ESP32-S3 固件（Arduino）和刷写脚本
server/     Agent、网页界面、屏幕采集和板子连接（Python，FastAPI）
            serve/：可选的极简服务，用来自己部署 Hugging Face 视觉模型
```

## 路线图

- [ ] 测试基于 DiffusionGemma 的纯本地方案（`server/serve/` 里有实验性的 OpenAI 兼容服务）
- [ ] 支持横屏 App（目前坐标和校准都按竖屏计算）
- [ ] 非 ASCII 文字输入（例如通过 iOS 拼音键盘输入中文）
- [ ] 更多常用 App 的示例任务

## 在外使用板子（WireGuard，可选）

板子通过 Wi-Fi 连接服务端，默认必须和服务端在同一网络。要在别处使用手机和板子（画面本身已可经 VPN 传输），可以让板子
连接第二个网络，并通过 WireGuard 隧道连到服务端；这样板子的连接在路上也是加密的。

- 在 `secrets.h` 里设置 `WIFI_SSID2`/`WIFI_PASS2`（例如开启“最大兼容性”的 iPhone 个人热点：板子只支持 2.4 GHz
  WPA2/WPA3 个人级，不支持需要网页登录的网络）以及 `secrets.example.h` 里的 `WG_*`。`SERVER_HOST` 改为服务端在隧道内的地址，然后重新烧录。
- 在有公网地址的 WireGuard 中心节点上把板子加为对端（`allowed-ips <WG_ADDRESS>/32`），只允许它访问服务端隧道地址的板子端口
  （7300）；服务端也要为隧道放行该端口。
- 板子先等待时间同步（NTP，WireGuard 需要），再建立隧道，然后照常连接。第一个网络 15 秒连不上就换第二个，反之亦然。
  不设置 `WG_*` 时直接连接 `SERVER_HOST`。

隧道密钥和 Wi-Fi 密码一样以明文存在板子的 flash 里：板子丢了就要在中心节点上删除它的对端。

## 限制

- iOS 不镜像部分界面（输入密码、Apple Pay、有版权保护的视频），Agent 看不到这些画面。
- 手机锁屏后镜像会断开，Agent 工作时请关闭自动锁定。
- 键盘只支持 ASCII 输入。
- 修改辅助触控的跟踪速度后需要重新校准。
- 暂不支持横屏 App：在横屏界面里的点按和绘图位置会偏。
- 在有多个网卡的 Linux 机器上，如果手机列表里看不到接收端，把 Avahi（mDNS）限制在手机所在网络的网卡上。

## 安全

Agent 能做你在解锁手机上能做的任何事，所以对它的访问有保护：

- **网页** —— 所有页面和 API 都需要网页 token（打开 `/?token=…` 后设置的 `SameSite=Strict`、`HttpOnly` cookie，
  或脚本使用的 `Authorization: Bearer` 请求头；至少 16 个字符）。浏览器表明来自其他来源（`Origin` / `Sec-Fetch-Site`）
  的修改请求（`POST`）也会被拒绝，这也覆盖了同一主机上共享 cookie 的其他端口。由此挡住 CSRF 和 DNS 重绑定攻击。
  token 不会写进访问日志。只需要本机访问时设置 `HOST=127.0.0.1`。
- **板子连接** —— 服务端只接受能证明知道 `DEVICE_KEY` 的板子，板子也只接受能证明同一密钥的服务端，之后的每条命令和回复
  都带有本次连接的标签。陌生设备连到板子端口既不能操控手机，也不能把真正的板子挤掉；即使有人截获板子的连接（例如 ARP
  欺骗）并转发握手，也无法注入或重放命令。连接没有加密：网络里的人能看到命令内容，包括输入的文字——除非走可选的 WireGuard 隧道（见“在外使用板子”）。
- **AirPlay** —— 新手机需要输入配对码，输过的手机记录在 `~/.hidagent/uxplay.register`，网络里的其他设备无法给 Agent
  喂假画面。配对码是固定的 4 位数字，这里没有验证 UxPlay 是否限制猜错次数，所以请让接收端处在可信的网络里。
- `~/.hidagent` 里的密钥（目录 `0700`、文件 `0600`，从不通过符号链接写入）只有你的用户可读。Wi-Fi 密码和 `DEVICE_KEY`
  以明文存在板子的 flash 里：板子丢了就当密钥已泄露，两者都要更换。

仍需你自己注意：模型会照着屏幕上的内容行动，包括别人写的文字（网页或消息可能试图给它下指令）。最好用一台没有支付和银行
App 的备用手机，看着它工作，紧急停止按钮随时备好。使用云端模型时，每张截图都会发送给模型服务商。

## 合理使用

只自动化你自己拥有或被允许使用的设备和账号，并遵守相关 App 的条款。Agent 是在替你动手：请看着它，紧急停止按钮随时备好。

## 参与贡献

欢迎提 Issue 和 Pull Request。提交前请先跑一遍检查：

```bash
cd server && pip install -e ".[dev]" && ruff check . && pytest
```

## 相关项目

其他用代码或 AI 操作 iPhone 的方案，以及与本项目的区别。如有错误欢迎指正。

| 项目 | 做法 | 手机 / 主机需要 | 与本项目的区别 |
|---|---|---|---|
| [VersaiOS](https://github.com/zhaodashuaibi/VersaiOS) | UxPlay 镜像 + ESP32-S3 蓝牙鼠标 + 视觉模型 | ESP32 通过 USB 串口连电脑；桌面 GUI，主要是 Windows | 思路最接近。一条指令点一次，没有规划步骤，也不比对前后画面 |
| [ja1chen/actell（执言）](https://github.com/ja1chen/actell) | iPhone 上装 agent App，通过苹果 CoreDevice 调试通道截屏和点按 | 开发者模式、配对 + Developer Disk Image、Xcode 自签 | 本项目不需要开发者模式，不用在手机上装 App，也不用签名 |
| [AidenAI-IO/aiden-firmware](https://github.com/AidenAI-IO/aiden-firmware) | 专用板：HDMI 采集 + USB 键鼠 + 多模态模型 | HDMI 采集芯片和扩展坞；以 Android 为主 | 本项目用 AirPlay 代替 HDMI 采集，不需要采集硬件 |
| [yoyicue/glassbox](https://github.com/yoyicue/glassbox) | PicoKVM HDMI 采集 + USB HID，观察/决策/执行/验证循环 | macOS 控制端和 PicoKVM | 本项目在 Linux 上运行，只要一块 ESP32 |
| [boboabw/phone-agent](https://github.com/boboabw/phone-agent) | ESP32-S3 蓝牙 HID + 辅助触控，agent 是 iPhone App | 需要 Mac + Xcode 编译签名 | 本项目手机上什么都不用装 |
| [dovydaskel/Iphone-control-for-social-media](https://github.com/dovydaskel/Iphone-control-for-social-media) | ESP32 蓝牙鼠标 + OpenCV 指针校正，脚本化发帖 | Mac | 指针闭环思路相近；本项目多了视觉模型的规划/执行 |
| [VKCOM/devicehub](https://github.com/VKCOM/devicehub) | ESP32 蓝牙鼠标，用于 iOS 设备农场 | 设备农场基础设施 | 设备农场工具，没有 AI agent |
| [NolanCotter/AirBridge](https://github.com/NolanCotter/AirBridge) / [gbulog/pcairplay](https://github.com/gbulog/pcairplay) | Linux 上的 UxPlay + 电脑自带蓝牙模拟键鼠 | 带蓝牙的 Linux 电脑 | 人工远程操作，没有 AI |

其他路线：基于 WebDriverAgent/XCUITest 的工具（如 [mobile-mcp](https://github.com/mobile-next/mobile-mcp)、[Midscene](https://github.com/web-infra-dev/midscene)）需要 Mac 和代码签名；基于 macOS iPhone 镜像的工具需要 Mac。

## 致谢

[UxPlay](https://github.com/FDH2/UxPlay)（AirPlay 接收端） ·
[Arduino ESP32 core](https://github.com/espressif/arduino-esp32) 和 [TinyUSB](https://github.com/hathach/tinyusb)（USB HID） ·
[FastAPI](https://fastapi.tiangolo.com/)

## 许可证

[MIT](LICENSE)
