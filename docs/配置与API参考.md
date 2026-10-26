# 配置与 API 参考

本文档收录 `letv_daemon` 的配置字段、Web 控制台、CLI 与 JSON API 细节。
功能介绍与部署步骤见根目录 [readme.md](../readme.md)。

---

## 1. 配置

电视参数必须通过独立配置文件调整，代码中不得硬编码。

配置文件核心字段（完整示例见仓库根目录 [config.json](../config.json)）：

```json
{
  "device": {
    "host": "192.168.0.1",
    "adb_port": 5555,
    "reconnect_interval": 5,
    "command_timeout": 10,
    "poll_interval": 5,
    "offline_after_sec": 60,
    "offline_poll_interval": 30,
    "offline_confirm_polls": 3,
    "adb_path": "adb",
    "transport": "auto"
  },
  "udp": {
    "enabled": true,
    "host": "192.168.0.1",
    "port": 9900,
    "timeout": 2
  },
  "watchdog": {
    "enabled": true,
    "target": "com.peter.tvhome.letv/com.gathin.tvhome.MainActivity",
    "stock_package": "com.stv.deskplatform",
    "debounce_sec": 1.5,
    "poll_sec": 5,
    "pause_file": "PAUSE"
  },
  "web": {
    "host": "0.0.0.0",
    "port": 8770,
    "token": ""
  },
  "allow_dangerous_actions": true,
  "apps": [
    {
      "package": "com.gitvvideo.letv",
      "name": "银河奇异果",
      "activity": "com.gala.video.app.epg.HomeActivity"
    }
  ],
  "app_names": {
    "com.stv.lap.alitvbuy": "电视淘宝"
  },
  "inputs": [
    { "name": "HDMI 1", "action": "key", "key": "switch_hdmi_0" },
    {
      "name": "银河奇异果",
      "action": "app",
      "package": "com.gitvvideo.letv",
      "target": ["com.gitvvideo.letv", "com.gitvvideo.letv/.MainActivity"]
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `device.transport` | `auto`（默认）/ `adbutils` / `subprocess` |
| `device.poll_interval` | 状态轮询间隔，**最小 2 s**（dumpsys 单次约 1.3 s） |
| `device.offline_after_sec` | 连续离线多少秒后开始降频，默认 `60`；`0` 表示一离线就降频 |
| `device.offline_poll_interval` | 降频后的采样间隔，默认 `30` 秒；`0` 表示不降频。小于 `poll_interval` 时按 `poll_interval` 处理 |
| `device.offline_confirm_polls` | 连续多少次探测失败后记录一条"关机/离线"事件，默认 `3`；期间的单次失败只写 DEBUG 日志 |
| `udp.enabled` | 是否启用 UDP 9900；默认 `true`，关闭后关机/切源动作返回 503 |
| `udp.host` | UDP 目标地址；缺省跟随 `device.host` |
| `udp.port` | UDP 端口，默认 `9900` |
| `udp.timeout` | socket 超时，默认 `2` 秒 |
| `web.port` | 设为 `0` 时由系统分配临时端口（便于测试） |
| `web.token` | 非空时，所有 `/api/*` 请求需携带令牌 |
| `apps[].activity` | 可选；缺省时用 MAIN + LEANBACK/LAUNCHER/HOME Intent 自动解析 |
| `inputs` | 可选；`letv_tv` 的完整输入源列表（顺序即 HomeKit 顺序）。每项含 `name`、`action`（`app`/`key`）、`package`/`activity` 或 `key`、`target`（包名或 `package/activity`，可数组）。缺省时回退为 `HDMI 1` + `apps[]` |
| `app_names` | 可选；包名 → 中文显示名，用于 Web「应用管理」列表；`apps[].name` 作为回退 |
| `allow_dangerous_actions` | 默认 `false`；打开后才允许电源/切源等 ADB 与 UDP 动作，以及卸载应用 |
| `watchdog.enabled` | 启动时是否自动开启桌面守护；Web 仍可手动启停 |
| `watchdog.target` | 要拉回的组件，必须是 `package/activity` |
| `watchdog.stock_package` | 原桌面包名；轮询发现它在前台时触发 |
| `watchdog.debounce_sec` | 去抖窗口，默认 1.5 s |
| `watchdog.poll_sec` | 兜底轮询间隔，默认 5 s；`0` 关闭 |
| `watchdog.pause_file` | PAUSE 文件路径；相对路径以配置文件目录为基准 |

`apps` 只应填写**当前已安装**的包名；设备上的应用会变化，可用
Web「应用管理」面板或 `GET /api/packages?third_party=1` 查询，再更新
`config.json`。`app_names` 只影响显示，不改变实际包名；未配置的名称
在网页上直接显示完整包名。

自动解析失败时（例如银河奇异果报「未找到可用的启动 Activity」），从
`adb -s <serial> shell dumpsys package <包名>` 的 Activity Resolver Table
找到主 `HomeActivity`，把完整类名填进 `activity`。本项目已为
`com.gitvvideo.letv` 配置 `com.gala.video.app.epg.HomeActivity`。

`host` 是 DHCP 地址，重启后可能变化；长期部署请在路由器上做静态绑定。
配置校验失败时会指出具体字段路径并退出（退出码 2）。

---

## 2. 运行方式与 CLI

### 2.1 启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt    # adbutils 可选；未安装时自动用 adb 命令行
```

日常启动使用 `run.sh`，它会进入项目目录、激活 `.venv`（若存在）并前台运行：

```bash
./run.sh                          # 读取 ./config.json，日志输出到终端
./run.sh --log-level DEBUG        # 额外参数原样传给 letv_daemon
LETV_DAEMON_CONFIG=/path/config.json ./run.sh
```

脚本是前台进程，`Ctrl-C` 退出，不会后台驻留。第一次 `Ctrl-C` 会停止 Web、守护、
状态轮询与 ADB 连接（各后台线程最多等待 1 s，通常 1 s 内退出）；若设备或子进程卡住，
再按一次 `Ctrl-C` 立即强制退出（退出码 130）。

其他常用命令：

```bash
python3 -m letv_daemon --check               # 连接并输出一次状态（只读）
python3 -m letv_daemon --key home            # 发送一个按键
python3 -m letv_daemon --launch com.gitvvideo.letv
python3 -m letv_daemon --launch com.peter.tvhome.letv \
        --activity com.gathin.tvhome.MainActivity
python3 -m letv_daemon -c /path/config.json --log-level DEBUG --log-file letv.log
```

启动后访问 `http://<主机IP>:8770/`。Web 服务监听地址与端口由 `web.host` / `web.port` 决定。

### 2.2 控制台功能

无依赖（标准库 `http.server`）的单页界面：

- 遥控器键盘：方向、确定、主页、返回、菜单、音量、频道、数字键；
- 长按方向/音量键时，浏览器按固定间隔**逐次**发送请求，服务端不做自动重复；
- 快捷启动列表来自 `config.json` 的 `apps`；
- **应用管理**：列出电视上的第三方应用（完整包名；`app_names` 或
  `apps[].name` 可提供中文名），每项带「卸载」按钮并二次确认。
  清单只在 ADB 连接成功时自动获取一次，**不做定时刷新**；只有点击
  「刷新清单」才会重新读取设备。卸载走 `pm uninstall`，需要
  `allow_dangerous_actions=true`；桌面守护目标与原桌面包禁止卸载；
- 每 3 s 轮询一次状态，显示在线状态、屏幕开关、当前前台 App；
- 桌面守护面板：运行/暂停状态、已拉回次数、最近触发与错误，支持启动、停止、暂停、恢复；
- **电源 / 待机**按钮：走 ADB 发送 `power`（keyCode 26，待机/唤醒切换）；
- **关机**按钮：对外动作名 `power_off`，走 UDP 9900 实际发送 `power`（固件不认 `power_off`）；
- **信号源**卡片：走 UDP 发送 `switch_source`、`switch_hdmi_0`–`3`、`switch_tv`、
  `switch_vga`、`switch_cvbs`、`switch_station`、`switch_network_model`、
  `switch_bit_stream`、`switch_play_mode`；
- 以上动作都属于危险操作，需要 `allow_dangerous_actions=true`（否则 403），
  电源与关机按钮点击后还会二次确认；
- **安装 APK**：选择文件后直接上传，显示上传进度；服务端先校验 ZIP 头，
  再执行 `adb install -r`，页面展示 adb 的原始成功/失败输出；
- **上传壁纸**：支持 PNG / JPEG / WebP / GIF / BMP，按文件头识别真实格式，
  传到 `/sdcard/Android/data/com.peter.tvhome.letv/files/wallpapers`，
  文件名使用服务器本地操作时间（精确到微秒）；
- **桌面配置**：读取 `/sdcard/Android/data/com.peter.tvhome.letv/files/config.json`，
  在弹窗中格式化显示并支持一键复制；也可上传新的 `config.json`，
  服务端先校验 UTF-8 与 JSON 合法性（顶层必须是对象），再推送替换；
- 冷启动开机需外接硬件（WoL、红外、继电器），本项目只预留接口。

---

## 3. JSON API

HA 集成与其他客户端统一走这些接口：

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/api/health` | — | 服务版本、ADB 连接状态 |
| GET | `/api/state` | — | 设备状态；`?refresh=1` 强制立即采集 |
| GET | `/api/actions` | — | 当前允许的动作；`transport` 为 `adb`（含 keyCode）或 `udp` |
| GET | `/api/apps` | — | 配置中的快捷启动列表 |
| GET | `/api/inputs` | — | 配置的输入源：`name` `action`(`app`/`key`) `package` `activity` `key` `targets[]` |
| GET | `/api/volume` | — | 媒体绝对音量 `volume`（0–100）与 `max`；设备离线时直接返回 503（不访问设备） |
| GET | `/api/user-apps` | — | 缓存的用户应用清单（`package`、`name`、`protected`）；读取时不访问设备 |
| GET | `/api/packages` | — | 已安装包名；`?third_party=1` 只看第三方；每次请求都实时查询 |
| GET | `/api/launcher-config` | — | 读取自建桌面 `config.json`；返回原始 `content`、格式化 `pretty` 与 `valid` |
| GET | `/api/watchdog` | — | 桌面守护状态（运行、暂停、触发次数、最近错误） |
| POST | `/api/key` | `{"action":"home"}` | 发送一个动作；按名称自动选择 ADB 或 UDP，响应含 `transport` |
| POST | `/api/launch` | `{"package":"...","activity":"..."}` | 启动 App；`activity` 可省略，省略时用 intent 解析，返回的 `component` 在系统不回显组件时等于包名 |
| POST | `/api/volume` | `{"volume":0..100}` | 设置媒体绝对音量；通过音量键分级调整，返回实际生效的 `volume` 与 `max` |
| POST | `/api/user-apps/refresh` | `{}` | 重新读取 `pm list packages -3` 并更新缓存；失败返回 502/503 |
| POST | `/api/uninstall` | `{"package":"..."}` | 卸载用户应用（`pm uninstall`）；需 `allow_dangerous_actions=true`，仅接受当前缓存中的包，返回更新后的清单 |
| POST | `/api/apk` | APK 原始字节 | 安装 APK（`adb install -r`）；`X-Filename` 可选，用于回显文件名；上限 300 MiB |
| POST | `/api/wallpaper` | 图片原始字节 | 上传壁纸（`adb push`）；按内容识别格式，上限 20 MiB |
| POST | `/api/launcher-config` | `config.json` 原始字节 | 校验 UTF-8 编码与 JSON 对象后，经临时文件原子替换桌面配置；上限 1 MiB |
| POST | `/api/refresh` | `{}` | 立即采集并返回状态 |
| POST | `/api/watchdog` | `{"action":"start"}` | 启停/暂停守护；动作另有 `stop`、`pause`、`resume` |

`/api/state` 返回示例：

```json
{
  "online": true,
  "foreground_package": "com.peter.tvhome.letv",
  "foreground_activity": "com.gathin.tvhome.MainActivity",
  "foreground_component": "com.peter.tvhome.letv/com.gathin.tvhome.MainActivity",
  "foreground_app_name": "自建桌面",
  "screen_on": true,
  "screen": "on",
  "updated_at": 1792821472.05,
  "updated_at_iso": "2026-10-24T13:57:52+08:00",
  "error": null
}
```

`/api/user-apps` 与 `/api/user-apps/refresh` 返回同一结构：

```json
{
  "loaded": true,
  "updated_at": 1792832892.65,
  "updated_at_iso": "2026-10-24T17:08:12+08:00",
  "error": null,
  "protected": ["com.peter.tvhome.letv", "com.stv.deskplatform"],
  "apps": [
    {"package": "com.gitvvideo.letv", "name": "银河奇异果"},
    {"package": "com.stv.lap.alitvbuy", "name": null}
  ]
}
```

`name` 为 `null` 时网页显示完整包名。卸载成功后返回同样的结构，并额外带
`status`、`package`、`output`；失败时保留原缓存。

`/api/apk` 返回 `{"status":"ok","filename":"...","size":123,"output":"Success"}`；
`/api/wallpaper` 返回 `{"status":"ok","filename":"20261024_153012_123456.png",
"remote_path":"/sdcard/.../20261024_153012_123456.png","size":123,"output":"..."}`；
`/api/launcher-config` 的 GET 与 POST 都返回 `pretty`（缩进 2 空格的格式化文本），
POST 成功后可直接刷新弹窗内容。
三个上传接口都使用原始请求体（不是 multipart），必须带 `Content-Length`；
上传请求处理完后连接会关闭（不复用 keep-alive），避免拒绝请求时残留字节污染下一条请求。

桌面配置的写入流程为 `上传到 config.json.tmp` → `mv -f config.json.tmp config.json`，
任何一步失败都会尝试删除临时文件，不会留下半截配置；服务端只检查 JSON 语法与
顶层必须为对象，**不校验桌面 App 的字段含义**，字段写错仍会被原样推送。

错误统一返回 `{"error": "...", "code": "..."}`：400 参数/文件格式错误、401 未授权、
403 危险动作被拒、404 未找到、411 缺少 `Content-Length`、413 文件过大、
502 设备/UDP 发送失败或 APK 安装失败、503 设备不可用或 UDP 未启用。

鉴权：`web.token` 非空时，`/api/*` 需携带 `X-Token: <token>` 或
`Authorization: Bearer <token>`（也接受 `?token=`）。静态页面本身不需要令牌。
