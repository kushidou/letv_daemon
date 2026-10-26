#!/usr/bin/env bash
# 前台启动 letv_daemon：加载项目虚拟环境（若存在），然后运行 Web 控制台。
# 额外参数会原样传给 letv_daemon，例如:
#   ./run.sh --check
#   ./run.sh --log-level DEBUG
#   LETV_DAEMON_CONFIG=/path/config.json ./run.sh

set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

if command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    PYTHON=python3
fi

exec "$PYTHON" -u -m letv_daemon "$@"
