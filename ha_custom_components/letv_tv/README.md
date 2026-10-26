# Home Assistant 自定义集成：letv_tv

把乐视电视以 **HomeKit 电视（Television）** 的形式暴露给 Home Assistant，
并把「输入源」完全交给守护进程 `config.json → inputs` 配置：每个输入源
声明名称、动作（启动 App 或发送按键）以及用于状态回读的目标包名/组件名。
选中某个输入源即执行对应动作，当前前台 App 反向映射为当前输入源。

本集成与 [`letv_daemon`](../letv_daemon/) 集成**并存**，不修改后者的实体。
它只负责一个 `media_player` 实体：

```
HomeKit ──> media_player.<电视> ──HTTP──> letv_daemon (8770) ──ADB/UDP──> 电视
```

HomeKit 需要 `device_class: tv` 且走 **accessory 模式**才能生成完整的
Television 配件（category=31，含 InputSource、TelevisionSpeaker、RemoteKey）。
桥模式会静默跳过电视设备。

---

## 1. 文件布局

```
ha_custom_components/
└── letv_tv/                  # 复制到 HA 的 custom_components/letv_tv/
    ├── __init__.py           #   配置条目生命周期、YAML 导入
    ├── api.py                #   HTTP 客户端（不依赖 homeassistant）
    ├── config_flow.py        #   UI 配置流 / 重新认证 / 选项
    ├── coordinator.py        #   状态与音量轮询
    ├── entity.py             #   实体基类、设备信息
    ├── homekit_compat.py     #   HomeKit 绝对音量/回写的运行时兼容补丁
    ├── media_player.py       #   唯一的电视实体、关机状态保持、set_volume 服务
    ├── sources.py            #   输入源构造、target 匹配、遥控映射、乐观源跟踪
    ├── const.py
    ├── services.yaml         #   自定义服务 letv_tv.set_volume 的界面描述
    ├── manifest.json
    ├── strings.json
    └── translations/         #   en / zh-Hans
```

## 2. 前置条件

1. `letv_daemon` 已运行，并且版本包含以下接口：

   ```bash
   curl -s http://127.0.0.1:8770/api/health
   curl -s http://127.0.0.1:8770/api/volume     # {"volume": 35, "max": 100}
   curl -s -X POST http://127.0.0.1:8770/api/volume \
        -H 'Content-Type: application/json' -d '{"volume": 35}'
   ```

2. `config.json → inputs` 定义了完整的输入源列表：

   ```json
   {
     "inputs": [
       { "name": "HDMI 1", "action": "key", "key": "switch_hdmi_0" },
       { "name": "桌面", "action": "app",
         "package": "com.peter.tvhome.letv",
         "activity": ".MainActivity",
         "target": "com.peter.tvhome.letv" },
       { "name": "爱奇艺", "action": "app",
         "package": "com.gitvvideo.letv",
         "target": ["com.gitvvideo.letv",
                    "com.gitvvideo.letv/.MainActivity"] }
     ]
   }
   ```

   - `name`：HomeKit 中显示的源名称，必须唯一；
   - `action`：`app`（启动应用）或 `key`（发送按键动作）；
   - `app` 需要 `package`，可选 `activity`（省略时由守护进程解析）；
   - `key` 需要 `key`，取值为守护进程的动作名；
   - `target`：一个包名/组件名或它们的数组。前台包名（含 `/` 时按完整
     组件名）匹配到 target 就把该源视为当前源。`app` 未写 target 时默认
     使用 `package`；`key` 类源（如 HDMI）没有物理回读时可省略。

   **输入源完全来自这里**，集成不硬编码任何 App，也不会自动追加 HDMI。
   不配置 `inputs` 时守护进程退回旧行为（`HDMI 1` + `apps[]`）。
   修改 `config.json` 后需重启守护进程，再重载本集成（源列表在集成加载时
   快照一次）。

3. 守护服务的 `allow_dangerous_actions=true`，否则不会出现
   `switch_hdmi_0`、`power_off` 等动作。

## 3. 安装

```bash
cp -r ha_custom_components/letv_tv \
      /home/kushidou/docker/hass/config/custom_components/
docker restart hass
```

新增自定义集成必须重启 Home Assistant。

### 3.1 通过 YAML 添加

```yaml
letv_tv:
  host: 127.0.0.1      # 可选，默认 127.0.0.1
  port: 8770           # 可选，默认 8770
  token: ""            # 对应 config.json → web.token，可为空
  name: 乐视电视        # 设备显示名，决定实体 ID 前缀
  # scan_interval: 5   # 可选，3–300 秒
```

YAML 导入**不访问守护进程**，因此守护服务重启时 HA 也能正常启动；
连接失败只会在实体加载时重试。改了 YAML 中的 host/port 会产生新条目，
旧条目需要到 UI 删除。也可以完全通过「设置 → 设备与服务 → 添加集成 →
LeTV TV」配置，效果相同。

### 3.2 添加 HomeKit 电视

在 `configuration.yaml` 中增加一个 **accessory 模式**的 HomeKit 条目：

```yaml
homekit:
  - name: Letv TV
    port: 21065
    mode: accessory
    filter:
      include_entities:
        - media_player.le_shi_dian_shi
```

实体 ID 由 `name` 生成（`乐视电视` → `le_shi_dian_shi`）；若名称不同，
以「开发者工具 → 状态」里实际出现的 ID 为准。

重启后用 Home App 扫描该条目的配对二维码。若已有桥条目，桥内的电视会被
跳过，必须单独添加 accessory 条目。

## 4. 实体

| 实体 | 说明 |
|---|---|
| `media_player.<name>` | 唯一的电视实体，`device_class: tv` |

支持的 feature：

| Feature | 行为 |
|---|---|
| `TURN_OFF` | 发送 `power_off`（必要时退化 `power`）；已确认关屏时不再发送 |
| `TURN_ON` | **空操作**，开机由独立硬件实现 |
| `SELECT_SOURCE` | 选择配置的输入源，见 §5 |
| `VOLUME_STEP` | 相对音量 +/− |
| `VOLUME_MUTE` | 静音切换 |
| `VOLUME_SET` | 绝对音量；仅当 HomeKit 兼容补丁安装成功时声明 |
| `PLAY` / `PAUSE` | 发送 `media_play` / `media_pause`，HomeKit 播放暂停键发送 `media_play_pause` |

HA 2025.8.2 的 HomeKit Television 有两处音量问题：把 HomeKit 的 0–100
音量值原样传给只接受 0–1 的 `media_player.volume_set`，以及不回写当前
音量。集成在 `homekit_compat.py` 中于运行时替换 HomeKit 的
`TelevisionMediaPlayer` 实现，正确换算标度并回写 Volume 特征；补丁安装
失败（HA 内部结构变化）时不声明 `VOLUME_SET`，自动退回相对音量。

附加状态属性便于排查：`daemon_online`、`foreground_app_name`、
`foreground_package`、`volume_max`。

## 5. 输入源与状态跟踪

源列表与顺序完全取自 `config.json → inputs`（经 `GET /api/inputs`）。
重名时保留第一个（HomeKit 要求源名称唯一）。

选择源时：

- `action: app`：`POST /api/launch`（package + 可选 activity）；
- `action: key`：发送 `key` 指定的动作（例如 `switch_hdmi_0`）。

选中后会立即写入乐观状态，App 源在约 1.5 秒后强制刷新一次，避免切换前
的旧前台读数把源抢回去。之后的轮询按 target 匹配前台包名/组件名：

- 前台匹配到某个源，且没有乐观覆盖 → 直接采用；
- 有乐观覆盖时：
  - 前台匹配到目标源 → 清除覆盖；
  - 前台仍是选择前的源（陈旧读数）→ 保持乐观值；
  - 前台匹配到另一个源 → 认为用户从别处切换，清除覆盖并跟随；
- 前台为 `null` 或不匹配任何 target（例如系统控制界面）→ 保持当前源。

**手动操作电视也会更新状态**：例如在电视上直接打开爱奇艺，只要前台包名
命中爱奇艺的 target，HA 与 HomeKit 的输入源会随之切换，无需经过 HA。

没有回读 target 的源（例如 HDMI）选择后会一直保持，直到出现另一个可
匹配的源。HA 重启后乐观状态丢失，以当时的真实前台为准；无法匹配时
HomeKit 回退到源列表第一项。

## 5.1 关机后的状态

电源状态始终与设备实际读数同步，不做乐观覆盖：

- `screen_on` 为 `true`/`false` 时直接映射为 `on`/`off`；
- 电视关机后 ADB 端口关闭，守护进程只能报告 `online=false`、`screen_on=null`。
  此时保留上一次状态，连续 3 次轮询（默认约 15 秒）都采集不到设备才判定为
  `off`，短暂网络抖动不会误报；实体不会变成 “unknown”；
- 关机前后**输入源、音量等状态保持不变**，只是电源状态变为 `off`；
- 再次开机后轮询恢复，状态回到 `on` 并继续跟随前台输入源。

## 6. 绝对音量

兼容补丁生效时，Home App 的音量滑块直接写入绝对音量；遥控器音量 +/−
也可以使用。所有写入都通过 `POST /api/volume` 合并设置，快速连续调整
只保留最新目标值。兼容补丁不可用时退回相对步进（每次约 1%）。
音量轮询失败时保留上一次的值，不会让电视实体变成不可用。

HA 侧可调用自定义服务（0–100）：

```yaml
action: letv_tv.set_volume
target:
  entity_id: media_player.le_shi_dian_shi
data:
  volume: 30
```

设置大音量差时守护进程会分批注入音量键，最长可能需要几十秒（POST 超时
120 秒），期间实体显示的是乐观值，完成后替换为设备实际值。

静音是切换键，实体维护乐观的 `is_volume_muted` 状态；状态未知时收到
「取消静音」只更新状态、不盲发按键。使用实体遥控器静音后，HA 侧状态
可能与电视不一致，这是缺少静音回读的固有限制。

## 7. HomeKit 遥控器按键

电视配件生成 RemoteKey 服务，按键通过事件
`homekit_tv_remote_key_pressed` 转给本实体：

| HomeKit key_name | 守护进程动作 |
|---|---|
| `arrow_up` / `arrow_down` / `arrow_left` / `arrow_right` | `up` / `down` / `left` / `right` |
| `select` | `ok` |
| `back` | `return` |
| `exit` | `home` |
| `information` | `info` |
| `play_pause` | `media_play_pause` |
| `next_track` / `previous_track` | `media_next` / `media_previous` |
| `rewind` / `fast_forward` | `media_rewind` / `media_fast_forward` |

播放/暂停在实体支持 `PLAY|PAUSE` 时由 HomeKit 直接调用媒体播放服务，
其余按键走事件总线；发送失败只写日志，不会向事件总线抛异常。

## 8. 排障

```bash
# 集成日志
docker logs -f hass 2>&1 | grep -i letv_tv

# 守护服务状态
curl -s http://127.0.0.1:8770/api/state  | python3 -m json.tool
curl -s http://127.0.0.1:8770/api/inputs | python3 -m json.tool
curl -s http://127.0.0.1:8770/api/volume
```

| 现象 | 检查 |
|---|---|
| 实体不可用 | 守护服务是否可达；`/api/health` 是否 200；token 是否正确 |
| 输入源列表为空或旧 | `config.json → inputs` 是否有效；是否重启守护进程并重载集成 |
| 手动切 App 后源不更新 | 该 App 的前台包名是否包含在对应源的 `target` 中 |
| HomeKit 没有音量滑块 | 日志确认“已安装 HomeKit 绝对音量兼容补丁”；失败时退回相对音量 |
| HomeKit 看不到电视 | HomeKit 条目是否 `mode: accessory`；实体是否为 `device_class: tv` |
| HomeKit 源序号与预期不符 | 源顺序以本集成日志/属性中的列表为准，HomeKit 按索引映射 |
| 音量不动 | 守护服务是否支持 `/api/volume`；电视是否 ADB 在线；查看 daemon 日志 |
| 选了 App 又跳回旧源 | 前台 App 是否真的切换；跨 App 启动可能需要更长 settle 时间 |
| 选 HDMI 后源不变 | 预期行为：HDMI 无物理回读，需切到某个已知 App 才会离开 |

## 9. 已知限制

1. **HomeKit 绝对音量依赖运行时补丁**：HA 升级若改变 HomeKit 内部结构，
   补丁会安全失败并退回相对音量；HA 内始终可用 `letv_tv.set_volume`。
2. **没有 target 的源无回读**：例如 HDMI，选择后状态是乐观值。
3. **源列表只在启动时读取**：改 `config.json` 后需重启守护进程并重载集成。
4. **开机为空操作**：冷启动由外部硬件负责。
5. **危险动作需放行**：`switch_*`、`power_off` 依赖守护服务的
   `allow_dangerous_actions=true`。
6. **状态延迟**：守护服务约 5 秒采集一次，HA 默认再按 5 秒轮询，
   最坏约 10 秒；动作后会立即强制刷新一次。
