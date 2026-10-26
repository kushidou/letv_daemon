# 乐视电视维护与桥接器（letv_daemon）

通过 ADB（少量动作经 UDP 9900）远程控制与监控乐视超级电视，并提供Web 控制台、Home Assistant 集成与 HomeKit 电视集成。

适用设备：**乐视超级电视 X4-50**（Android 6.0，ADB `5555`，UDP `9900`）。

---

## 功能

| 功能 | 说明 |
|---|---|
| 遥控与启动 | 模拟遥控按键、启动已安装 App、绝对音量调节（0–100） |
| 电源与切源 | 关机、TV / HDMI / AV / VGA 切源；ADB 无法完成的动作由 UDP 补位；**不支持开机** |
| 状态监控 | 当前前台 App、屏幕开关、ADB 在线状态、音量 |
| 桌面守护 | 监听 HOME 事件并拉起自定义桌面软件（推荐[cleanyourtv(Modified for LeEco TV)](https://github.com/kushidou/cleanyourtv/tree/main)) |
| Web 控制台 | 遥控器、快捷启动、应用管理、APK 安装、壁纸与桌面配置上传 |
| JSON API | 供 HA 集成与自动化脚本调用 |
| HA 集成与HomeKit 电视 | `letv_daemon`：电视、电源、按键、信号源、音量等实体；`letv_tv`：单个电视实体，**App 即输入源**，支持绝对音量与遥控按键 |

> 电视关机后会断开网络，因此无法通过网络开机。

---

## 部署

### 1. 环境准备

- 常开的 Linux 主机（树莓派 / NAS / 软路由均可）
- Python ≥ 3.8
- 主机已安装 `adb`，且能连接电视：`adb connect <电视IP>:5555`
- 电视已开启 ADB 无线调试，并与主机处于同一局域网

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt    # adbutils 可选；未装时回退 adb 命令
```

### 2. 配置

将[config_example.json](config_example.json)重命名为 config.json，并调整配置，至少需要调整：

| 字段 | 说明 |
|---|---|
| `device.host` | 电视 IP，建议在路由器上做 DHCP 静态绑定 |
| `web.host` / `web.port` | Web 服务监听地址，默认 `0.0.0.0:8770` |
| `web.token` | 内网可留空；非空时所有 `/api/*` 请求需携带令牌（未测试） |
| `allow_dangerous_actions` | 关机、切源、卸载应用需设为 `true` |
| `apps` | Web 控制台的快捷启动列表 |
| `inputs` | `letv_tv` 的输入源（名称、动作、状态回读目标） |

完整字段说明见 [配置与 API 参考](docs/配置与API参考.md)。

### 3. 启动

```bash
./run.sh                    # 前台运行，Ctrl-C 退出
```

启动后访问 `http://<主机IP>:8770/` 打开 Web 控制台。

### 4. 注册为 systemd 服务（推荐）

首先修改 `letv-daemon.service` 里面的用户名和文件路径。

```bash
sudo cp letv-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now letv-daemon
systemctl status letv-daemon
```

### 5. 安装 Home Assistant 集成

将集成分别添加到 HA 的`custom_components`里：

```bash
# 普通安装
cp -r ha_custom_components/letv_daemon /path/to/home-assistant/config/custom_components/
cp -r ha_custom_components/letv_tv     /path/to/home-assistant/config/custom_components/

```

重启后：

1. 打开「设置 → 设备与服务 → 添加集成 → **LeTV Daemon**」，
   填写守护主机、端口（默认 `8770`）、令牌与设备名。
2. 如需 HomeKit 电视，在 `configuration.yaml` 中声明 `letv_tv`：

   ```yaml
   letv_tv:
     host: 127.0.0.1
     port: 8770
     name: 乐视电视
   ```

   并新增一个 **accessory 模式**的 HomeKit 条目（桥模式会跳过电视实体）：

   ```yaml
   homekit:
     - name: Letv TV
       port: 21070
       mode: accessory
       filter:
         include_entities:
           - media_player.le_shi_dian_shi
   ```

   重启 HA 后用 Home App 扫描配对二维码。实体 ID 由 `name` 生成
   （`乐视电视` → `media_player.le_shi_dian_shi`），以实际状态页显示为准。
   详细步骤与限制见 [letv_tv README](ha_custom_components/letv_tv/README.md)。

