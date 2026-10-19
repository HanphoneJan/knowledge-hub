#!/usr/bin/env bash
# 视觉查询服务的**独立部署脚本**（D204）。
#
# ## 为什么单独一个脚本，而不是 systemd
#
# 视觉服务常驻约 2.65GB 的 colSmol，**只在这台机器上跑才有意义**：
# 小内存机器（2C4G）根本不该装它。它跟不跟 API / worker 一起起，
# 是一个独立的部署决策（旧问题见 `.docs/HANDOFF.md`「视觉服务的部署形态」）。
# 结论：**用脚本**，不引入 systemd —— 与 `dev.sh` 共用同一套
# pid 文件 / 日志 / 扫残留的纪律，于是"独立起停"与"dev.sh 编排"
# 是**一份实现**（CONVENTIONS §0.1：不写第二份）。
#
# ## 用法
#
# ```bash
# bash deploy/visual.sh start     # 起（启动前会清掉残留旧进程）
# bash deploy/visual.sh stop
# bash deploy/visual.sh restart
# bash deploy/visual.sh status     # 看 pid / 端口 / 模型是否就绪
# bash deploy/visual.sh logs       # tail -f /tmp/pkh_visual.log
# ```
#
# ⚠️ 三个坑与 `dev.sh` 完全相同（`uv run` 派生的子进程不在 leader 进程组里）：
#   1. 只 kill pid 文件**杀不干净** → 必须按命令行特征扫残留；
#   2. 旧进程会继续占着 2.65GB 并用**旧代码**应答，而它"看起来是活的"；
#   3. **只绑 `127.0.0.1`**：它没有鉴权，绑 `0.0.0.0` 等于对外开放一个模型服务。
set -u
cd "$(dirname "$0")/.."

VISUAL_PORT="${PKH_VISUAL_PORT:-8020}"
RUN_DIR="${PKH_RUN_DIR:-/tmp/pkh}"
VISUAL_PID_FILE="$RUN_DIR/visual.pid"
VISUAL_LOG="/tmp/pkh_visual.log"
VISUAL_PATTERN='services\.visual_service\.main'
mkdir -p "$RUN_DIR"

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

# 列出除本 shell 之外、匹配模式的进程（与 dev.sh 的 _stale_by_pattern 同款）
_stale_by_pattern() {
    local pattern="$1" p
    for p in $(pgrep -f "$pattern" 2>/dev/null); do
        [ "$p" = "$$" ] && continue
        [ "$p" = "$PPID" ] && continue
        echo "$p"
    done
}

stop() {
    local pid p
    pid=$(cat "$VISUAL_PID_FILE" 2>/dev/null || true)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
    # 🔑 按命令行特征扫残留 —— `kill -- -PID` 杀不干净 `uv run` 的子进程
    #   （§0.10 的坑 #2；否则旧进程继续占着 2.65GB 并用旧代码应答）。
    for p in $(_stale_by_pattern "$VISUAL_PATTERN"); do
        kill "$p" 2>/dev/null && echo "停止视觉服务 (pid $p)"
    done
    sleep 2
    for p in $(_stale_by_pattern "$VISUAL_PATTERN"); do
        kill -9 "$p" 2>/dev/null && echo "强杀残留视觉服务 (pid $p)"
    done
    rm -f "$VISUAL_PID_FILE"
}

status() {
    local pid bound
    pid=$(cat "$VISUAL_PID_FILE" 2>/dev/null || true)
    bound=$(ss -ltnp 2>/dev/null | grep ":$VISUAL_PORT " \
            | grep -oP 'pid=\K[0-9]+' | head -1)
    echo "pid 文件: ${pid:-（无）} · 监听 :$VISUAL_PORT: ${bound:-（无）}"
    if curl -s --noproxy '*' "http://127.0.0.1:$VISUAL_PORT/health" 2>/dev/null \
        | grep -q '"model_loaded":true'; then
        echo "模型: 已加载 ✅（就绪）"
    else
        echo "模型: 未就绪（加载 colSmol 约 13s，或服务没起 —— 看 $VISUAL_LOG）"
    fi
}

case "${1:-start}" in
    stop)
        stop
        ;;
    restart|start)
        [ "${1:-start}" = restart ] && { stop; sleep 1; }
        # ⚠️ 启动前扫残留：见文件头坑 #2（旧进程占着 2.65GB 并用旧代码应答）
        for p in $(_stale_by_pattern "$VISUAL_PATTERN"); do
            kill "$p" 2>/dev/null && echo "⚠️ 清掉残留视觉服务 (pid $p)"
        done
        # 🔴 --host 127.0.0.1：它没有鉴权，绑 0.0.0.0 等于对外开放 2.65GB 的模型。
        setsid uv run uvicorn services.visual_service.main:app \
            --host 127.0.0.1 --port "$VISUAL_PORT" \
            > /tmp/pkh_visual.log 2>&1 < /dev/null &
        echo $! > "$VISUAL_PID_FILE"
        echo "视觉查询服务已启动 :$VISUAL_PORT（pid $(cat "$VISUAL_PID_FILE")，"\
"日志 $VISUAL_LOG，加载 colSmol 约 13s）"
        ;;
    status)
        status
        ;;
    logs)
        tail -f "$VISUAL_LOG"
        ;;
    *)
        echo "用法: bash deploy/visual.sh {start|stop|restart|status|logs}" >&2
        exit 2
        ;;
esac
