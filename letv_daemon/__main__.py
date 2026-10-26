"""命令行入口。

    python -m letv_daemon                  # 启动 Web 控制台
    python -m letv_daemon --check          # 连接并输出一次状态
    python -m letv_daemon --key home       # 发送一个按键
    python -m letv_daemon --launch tv.emby.embyatv
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Config, ConfigError, load_config
from .device.adb import ADBClient, AdbError
from .device.apps import UserAppCatalog
from .device.control import DeviceController, UDPUnavailableError
from .device.keys import DangerousActionError, UnknownActionError
from .device.state import StateMonitor
from .device.udp import UDPClient, UdpError
from .logging_setup import setup_logging
from .watchdog import Watchdog
from .web.server import WebServer

log = logging.getLogger(__name__)


def _build_client(config: Config) -> ADBClient:
    device = config.device
    return ADBClient(
        serial=device.serial,
        reconnect_interval=device.reconnect_interval,
        command_timeout=device.command_timeout,
        allow_dangerous=config.allow_dangerous_actions,
        adb_path=device.adb_path,
        transport_kind=device.transport,
    )


def _build_udp(config: Config) -> Optional[UDPClient]:
    if not config.udp.enabled:
        return None
    return UDPClient(
        config.udp.host,
        config.udp.port,
        timeout=config.udp.timeout,
    )


def _build_controller(
    config: Config, client: Optional[ADBClient] = None
) -> DeviceController:
    return DeviceController(
        client or _build_client(config),
        _build_udp(config),
        allow_dangerous=config.allow_dangerous_actions,
    )


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _watchdog_pause_path(config: Config) -> Path:
    """相对路径以配置文件所在目录为基准，便于 systemd/run.sh 启动。"""
    path = Path(config.watchdog.pause_file).expanduser()
    if path.is_absolute():
        return path
    base = Path(config.path).parent if config.path else Path.cwd()
    return base / path


def _build_signal_handler(stop_event: threading.Event, force_exit=None):
    """第一次信号优雅退出，第二次立即强制退出。

    设备无响应时，优雅退出仍可能卡在驱动/子进程上；保留一步强制退出，
    行为与常见的 CLI 工具（如 pytest）一致。
    """
    exit_func = force_exit or os._exit
    count = 0

    def _handle(signum: int, _frame: object) -> None:
        nonlocal count
        count += 1
        if count > 1:
            log.warning("再次收到信号 %s，强制退出", signum)
            exit_func(128 + signum)
            return
        log.info("收到信号 %s，准备退出；再次按 Ctrl+C 可强制退出", signum)
        stop_event.set()

    return _handle


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="letv_daemon",
        description="乐视电视 ADB 控制台与 Home Assistant 桥接器",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="配置文件路径（默认 ./config.json，可用 LETV_DAEMON_CONFIG 覆盖）",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    parser.add_argument("--log-file", default=None, help="可选日志文件路径")

    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check", action="store_true", help="连接设备并输出一次状态后退出"
    )
    actions.add_argument(
        "--key", metavar="ACTION", help="发送一个遥控按键后退出"
    )
    actions.add_argument(
        "--launch", metavar="PACKAGE", help="启动指定 App 后退出"
    )
    parser.add_argument(
        "--activity", default=None, help="配合 --launch 指定 Activity"
    )
    return parser


def _run_check(config: Config) -> int:
    client = _build_client(config)
    if not client.connect():
        log.error("无法连接设备 %s", config.device.serial)
        _print_json({"online": False, "serial": config.device.serial})
        return 1

    monitor = StateMonitor(
        client,
        config.device.host,
        config.device.adb_port,
        interval=config.device.poll_interval,
        offline_after_sec=config.device.offline_after_sec,
        offline_poll_interval=config.device.offline_poll_interval,
        offline_confirm_polls=config.device.offline_confirm_polls,
    )
    state = monitor.refresh()
    _print_json(state.as_dict())
    return 0 if state.online else 1


def _run_key(config: Config, action: str) -> int:
    controller = _build_controller(config)
    try:
        outcome = controller.send(action)
    except (DangerousActionError, UnknownActionError) as exc:
        log.error("%s", exc)
        return 2
    except UDPUnavailableError as exc:
        log.error("%s", exc)
        return 2
    except (AdbError, UdpError) as exc:
        log.error("按键发送失败: %s", exc)
        return 1
    finally:
        controller.close()
    _print_json(outcome.as_dict())
    return 0


def _run_launch(config: Config, package: str, activity: Optional[str]) -> int:
    client = _build_client(config)
    if not client.connect():
        log.error("无法连接设备 %s", config.device.serial)
        return 1
    try:
        component = client.launch_app(package, activity)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    except AdbError as exc:
        log.error("启动失败: %s", exc)
        return 1
    _print_json({"package": package, "component": component})
    return 0


def _run_server(config: Config) -> int:
    client = _build_client(config)
    client.start()

    monitor = StateMonitor(
        client,
        config.device.host,
        config.device.adb_port,
        interval=config.device.poll_interval,
        offline_after_sec=config.device.offline_after_sec,
        offline_poll_interval=config.device.offline_poll_interval,
        offline_confirm_polls=config.device.offline_confirm_polls,
    )
    # 重连成功后立即采样，避免离线退避让开机状态最多晚 30 秒才出现。
    client.add_connect_listener(monitor.wake)
    monitor.start()

    controller = _build_controller(config, client)
    if controller.udp_available:
        log.info(
            "UDP 遥控已启用: %s:%d", config.udp.host, config.udp.port
        )
    else:
        log.info("UDP 遥控已禁用（udp.enabled=false），关机/切源动作不可用")

    watchdog = Watchdog(
        client,
        config.watchdog,
        monitor,
        _watchdog_pause_path(config),
        serial=config.device.serial,
        adb_path=config.device.adb_path,
        reconnect_interval=config.device.reconnect_interval,
    )
    if config.watchdog.enabled:
        watchdog.start()
    else:
        log.info("配置中已禁用桌面守护（watchdog.enabled=false）")

    # 用户应用清单只在连接成功时自动刷新一次，不参与轮询。
    app_catalog = UserAppCatalog(client, config.display_name)
    app_catalog.start()

    web = WebServer(
        config,
        client,
        monitor,
        watchdog=watchdog,
        controller=controller,
        app_catalog=app_catalog,
    )
    try:
        web.start()
    except OSError as exc:
        log.error("无法启动 Web 服务: %s", exc)
        app_catalog.stop()
        watchdog.stop()
        monitor.stop()
        client.stop()
        controller.close()
        return 1

    urls: List[str] = [f"http://127.0.0.1:{web.port}/"]
    if config.web.host not in ("0.0.0.0", "::", ""):
        urls = [f"http://{config.web.host}:{web.port}/"]
    log.info("Web 控制台已启动: %s", "  ".join(urls))
    if config.web.token:
        log.warning("Web API 已启用访问令牌")

    stop_event = threading.Event()
    _handle_signal = _build_signal_handler(stop_event)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _handle_signal)
        except (ValueError, OSError):
            pass

    try:
        while not stop_event.wait(1.0):
            pass
    finally:
        log.info("正在停止服务…")
        web.stop()
        app_catalog.stop()
        watchdog.stop()
        monitor.stop()
        client.stop()
        controller.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.activity and not args.launch:
        parser.error("--activity 只能与 --launch 一起使用")

    try:
        setup_logging(args.log_level, args.log_file)
    except (ValueError, OSError) as exc:
        print(f"日志初始化失败: {exc}", file=sys.stderr)
        return 2

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    log.debug("配置文件: %s", config.path or "(默认)")

    if args.check:
        return _run_check(config)
    if args.key:
        return _run_key(config, args.key)
    if args.launch:
        return _run_launch(config, args.launch, args.activity)
    return _run_server(config)


if __name__ == "__main__":
    sys.exit(main())
