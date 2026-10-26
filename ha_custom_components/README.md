# Home Assistant 自定义集成：letv_daemon

> 另有 [`letv_tv`](letv_tv/README.md) 集成：把电视注册为 HomeKit Television，
> 用 App 作为输入源，并使用守护进程的绝对音量接口。两者可以并存。

把 `letv_daemon` 的 Web API 映射为 HA 实体。集成**不直接调用 ADB/UDP**，
所有设备操作都交给守护服务，从而复用它的连接锁、自动重连、危险动作
黑白名单与状态缓存。

```
HA ──HTTP──> letv_daemon (8770) ──ADB/UDP──> 乐视电视
```

---

## 1. 文件布局

```
ha_custom_components/
├── README.md                 # 本文档（安装步骤与 API 契约）
└── letv_daemon/              # 复制到 HA 的 custom_components/letv_daemon/
    ├── __init__.py           #   配置条目生命周期
    ├── api.py                #   HTTP 客户端（不依赖 homeassistant）
    ├── config_flow.py        #   UI 配置流 / 重新认证 / 选项
    ├── coordinator.py        #   轮询协调器
    ├── entity.py             #   实体基类、电源动作
    ├── binary_sensor.py      #   在线状态
    ├── button.py             #   关机、快捷启动
    ├── media_player.py       #   音量 / 静音 / 信号源
    ├── remote.py             #   遥控按键
    ├── select.py             #   信号源下拉框
    ├── sensor.py             #   当前应用
    ├── sources.py            #   switch_* 动作与显示名映射
    ├── switch.py             #   电源
    ├── manifest.json
    ├── strings.json
    └── translations/         #   en / zh-Hans
```

## 2. 安装

1. 确认 `letv_daemon` 已在常开主机上运行，并且 HA 能访问其 Web 端口
   （默认 `8770`）：

   ```bash
   curl http://<守护主机>:8770/api/health
   ```

2. 把整个 `letv_daemon` 目录复制到 HA 的 `custom_components/`：

   ```bash
   cp -r ha_custom_components/letv_daemon \
         /path/to/home-assistant/config/custom_components/
   ```

   Docker 部署可以直接用 `docker cp`：

   ```bash
   docker cp ha_custom_components/letv_daemon \
         hass:/config/custom_components/
   ```

3. 重启 Home Assistant（新增自定义集成必须重启，不能热加载）。

4. **设置 → 设备与服务 → 添加集成 → LeTV Daemon**，填写：

   | 字段 | 说明 |
   |---|---|
   | 主机 | `letv_daemon` 所在主机 IP / 主机名 |
   | 端口 | 默认 `8770` |
   | 访问令牌 | 可选；对应 `config.json → web.token` |
   | 设备名称 | 显示在 HA 中的名称，默认「乐视电视」 |

   保存时集成会访问 `/api/health` 验证连通性。即使电视当前处于待机
   （`connected=false`），配置也能保存，实体在电视恢复后自动变为可用。

5. 如需修改轮询间隔：集成条目 → **选项**。默认 5 秒，范围 3–300 秒。
   `web.token` 变化后，HA 会提示重新认证，输入新令牌即可。

## 3. 实体

| 实体 | 类型 | 能力 | 依赖的动作 |
|---|---|---|---|
| `sensor.<设备>_current_app` | sensor | 当前前台 App 名称；属性含包名、Activity、屏幕状态、错误 | — |
| `binary_sensor.<设备>_online` | binary_sensor | 守护服务是否仍能连上电视 | — |
| `switch.<设备>_power` | switch | 关机；`on` = 屏幕点亮（开机方向为空操作，见 §5） | `power_off` |
| `button.<设备>_power_off` | button | 关机；电视离线时仍可用（UDP） | `power_off`（退化为 `power`） |
| `select.<设备>_input_source` | select | 信号源：TV / HDMI 1–4 / AV / VGA | `switch_*` |
| `remote.<设备>_remote` | remote | `send_command` 发送任意动作；`turn_on` / `turn_off` | 同电源 |
| `media_player.<设备>` | media_player | 音量步进、静音切换、信号源选择 | `volume_up` / `volume_down` / `mute` / `switch_*` |
| `button.<设备>_<应用名>` | button | 每个 `config.json → apps` 项一个，点击启动 | — |

实体都挂在同一个设备下，设备信息包含守护服务地址，可直接跳转 Web 控制台。

**可用性约定**

- 守护服务不可达：所有实体 `unavailable`（协调器轮询失败）。
- 守护服务可达但电视离线（ADB 断开）：`binary_sensor` 显示 `off`；
  依赖 ADB 的实体（`remote`、`media_player`、应用按钮、`sensor`）
  `unavailable`；`button.<设备>_power_off`、`select.<设备>_input_source`
  以及提供 `power_off` 时的 `switch.<设备>_power` 仍然可用，
  因为它们走 UDP 9900，不经过 ADB。
- 电视离线时 `sensor` 不返回旧值，避免把缓存当成实时状态。

**信号源无法回读**：设备不提供当前信号源状态，`select` 与
`media_player` 在发送切源动作后显示乐观值；HA 重启后恢复为未知。

### 3.1 应用按钮的名称与清单

应用按钮**不固化在集成代码里**。集成启动时调用守护服务的 `/api/apps`，
列表与显示名来自守护服务的 `config.json → apps`：

```json
{
  "apps": [
    { "package": "com.gitvvideo.letv", "name": "银河奇异果",
      "activity": "com.gala.video.app.epg.HomeActivity" }
  ]
}
```

因此：

- 增删应用、改显示名都只需要改 `config.json`；
- 改完**重启 letv_daemon**，再在 HA 里重载集成（或重启 HA）才会生效；
- 集成只读取一次列表，运行期间不会自动感知守护服务的配置变化；
- 已创建的实体 ID 由首次出现的名称生成，改名后 ID 不一定跟着变。
  若只想改显示文字，也可以直接在 HA 中重命名实体，`unique_id`
  （`<entry>_launch_<package>`）保持不变，不会重建实体。

### 3.2 遥控动作名

`remote.send_command` 使用守护服务的动作名，与 Web 控制台一致，例如：

```yaml
action: remote.send_command
target:
  entity_id: remote.le_shi_dian_shi_yao_kong_qi
data:
  command:
    - home
    - right
    - ok
```

常用动作：`home` `return` `menu` `up` `down` `left` `right` `ok`
`volume_up` `volume_down` `mute` `channel_up` `channel_down` `num_0`–`num_9`
`media_play_pause` `media_play` `media_pause` `media_next` `media_previous`
`power` `power_off` 以及 12 个 `switch_*` 信号源动作。

完整列表以守护服务为准：

```bash
curl -s http://<守护主机>:8770/api/actions
```

`num_repeats` 会被逐次发送（上限 20），`delay_secs` 限制在 0–5 秒。
**UDP 动作（`power_off`、`switch_*`）不要设置 `num_repeats > 1`**，
重复包会重复执行。`power_off` 在 UDP 层实际发送的载荷是 `power`——
电视固件不认 `power_off`（2026-10-24 实测）。

### 3.3 音量与静音的限制

守护进程现在提供 `GET/POST /api/volume`，可以读取并设置媒体绝对音量，
但**本集成（`letv_daemon`）的 `media_player` 仍是早期的按键步进实现**：
它没有调用 `/api/volume`，因此 `volume_level` 始终未知，HA 只显示音量
+/- 按钮。需要绝对音量与 HomeKit 电视形态请使用
[`letv_tv`](letv_tv/README.md) 集成。

静音也是切换键。实体维护一个乐观的 `is_volume_muted` 状态：

- 初始为未知；HA 首次执行「静音」时发送一次切换键；
- 之后 HA 根据自己维护的状态决定是否再发切换键；
- **HA 重启或使用实体遥控器静音后，HA 侧状态可能与电视不一致**，
  此时按一次静音可能会反向切换。这是缺少状态回读的固有限制。

## 4. 调用的 API 契约（v0.3.0 冻结）

HA 集成使用以下 9 个接口。字段、状态码变更需同步修改
`ha_custom_components/letv_daemon/api.py`、`letv_tv/api.py` 与本文件。

`letv_daemon` 集成使用 health / state / actions / apps / key / launch；
`letv_tv` 另外使用 inputs 与音量接口。

| 方法 | 路径 | 请求体 | 成功响应（200） |
|---|---|---|---|
| GET | `/api/health` | — | `status` `version` `connected` `serial` |
| GET | `/api/state` | — | `online` `foreground_package` `foreground_activity` `foreground_component` `foreground_app_name` `screen_on` `screen` `updated_at` `updated_at_iso` `error` |
| GET | `/api/actions` | — | `actions[]`，每项含 `action` `label` `transport`(`adb`/`udp`) `dangerous`，ADB 项另有 `keycode` |
| GET | `/api/apps` | — | `apps[]`，每项含 `package` `name` `activity`（可为 `null`） |
| GET | `/api/inputs` | — | `inputs[]`，每项含 `name` `action`(`app`/`key`) `package` `activity` `key` `targets[]`，未用到的字段为 `null` |
| GET | `/api/volume` | — | `volume`（0–100 整数） `max` |
| POST | `/api/key` | `{"action":"home"}` | `action` `label` `transport` `keycode?` |
| POST | `/api/launch` | `{"package":"...","activity":"..."}`，`activity` 可省略 | `package` `component` |
| POST | `/api/volume` | `{"volume":0..100}` | `volume`（实际生效值） `max` |

`POST /api/volume` 通过音量键逐步调整，较大差值可能需要数十秒；
`volume` 必须是整数（不接受字符串或布尔），越界返回 400，ADB 错误
分别映射为 502/503。

状态字段约束：

- `online` 必为布尔；`screen_on` 可为 `true` / `false` / `null`；
- `updated_at` 为 Unix 时间戳（秒，浮点），`updated_at_iso` 为带时区的字符串；
- `foreground_app_name` 可能为 `null`（包名不在 `apps` 中）；
- `error` 正常情况下为 `null`，采集失败时为字符串；
- 集成对缺失/类型不符的可选字段一律降级为 `None`，不抛异常。

认证：`web.token` 非空时，请求带 `X-Token: <token>`。

错误码与集成行为：

| HTTP | `code` | 集成处理 |
|---|---|---|
| 401 | `unauthorized` | `LetvAuthError` → 触发 HA 重新认证流程 |
| 403 | `forbidden` | `LetvForbiddenError` → 实体动作报错「被守护服务拒绝」 |
| 400 | `bad_request` | `LetvApiError` → 实体动作报错 |
| 502 | `bad_gateway` | `LetvApiError` → 动作报错 / 轮询失败标记不可用 |
| 503 | `unavailable` | `LetvApiError` → 同上 |
| 其他 | — | `LetvApiError` |

超时与重试：

- 健康检查 5 s；状态/动作表/音量读取 10 s；按键 20 s；启动 App 20 s；
  设置绝对音量 120 s（需逐级注入音量键）。
- **GET 在网络错误或超时时重试一次（间隔 0.5 s）**；
- **POST 绝不自动重试**，避免重复按键或重复切源；
- 协调器按「选项」中的间隔轮询；一次失败只把实体标记为不可用，
  下一次轮询自动恢复，不会重试风暴。

## 5. 已知限制

1. **待机断网**：电视待机后 Wi-Fi 断开，ADB 与 UDP 都不可用；
   HA 无法冷启动电视。因此 `switch.turn_on` / `remote.turn_on` 目前是
   **空操作**（只写 debug 日志），开机由独立硬件（WoL / 红外 / 继电器等）
   实现。
2. **危险动作由守护服务放行**：`power` / `power_off` / `switch_*` 等
   需要在守护服务 `config.json` 中设置 `allow_dangerous_actions=true`。
   未放行时，相关动作不会出现在 `/api/actions` 中，实体动作会返回明确错误。
3. **状态有延迟**：守护服务自身约每 5 秒采集一次；HA 轮询再叠加同样的
   间隔，最坏情况约 10 秒。电源/启动 App 动作后会立即强制刷新一次。
4. **静音无回读**：见 §3.3。信号源同样无回读，切源后显示乐观值。
5. **需要 HA 重启**：新增/升级自定义集成后必须重启，选项修改会自动重载条目。

## 6. 调试

```bash
# 集成日志
docker logs -f hass 2>&1 | grep letv_daemon

# 直接核对守护服务返回
curl -s http://<守护主机>:8770/api/state | python3 -m json.tool
curl -s http://<守护主机>:8770/api/actions | python3 -m json.tool
```

仓库内针对客户端的单元测试（宿主机无 aiohttp 时自动跳过）：

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```
