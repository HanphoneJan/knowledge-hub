#!/usr/bin/env bash
# 启动/停止 API + worker（阶段 1c 起在用）。
#
# ## 三个必须知道的坑（都实际踩过）
#
# 1. **不要 `pkill -f "uvicorn services.api.main"`** ——
#    `pkill -f` 匹配完整命令行，而执行本脚本的 shell 的 argv 里就含这个字符串，
#    结果把自己一起 SIGTERM 掉。
#
# 2. **`kill -- -PID` 杀不干净 `uv run` 的子进程** ——
#    `uv run` 会派生出真正执行 python 的子进程，它可能不在 leader 的进程组里。
#    实测：dev.sh 报"已停止 worker"，实际还有一个旧 worker 在跑，
#    它加载的是**修改前的代码**，于是报"不支持的格式: (无扩展名)"，
#    而代码明明已经改对了。**排查这个花了 20 分钟**。
#    所以这里除了按 pid 文件杀，还要按命令行特征扫一遍残留。
#
# 3. **两个 worker 同时跑时，旧代码那个会抢走任务** ——
#    症状和"代码没生效"一模一样。启动前先确认没有残留。
set -u
cd "$(dirname "$0")/.."

PORT="${PKH_API_PORT:-8010}"
# D158：视觉查询服务。它常驻 colSmol（+2.65GB），所以**端口与进程都独立** ——
# 杀掉它不会碰到 API，API 挂掉也不会让那 2.65GB 一直占着。
VISUAL_PORT="${PKH_VISUAL_PORT:-8020}"
RUN_DIR="${PKH_RUN_DIR:-/tmp/pkh}"
API_PID_FILE="$RUN_DIR/api.pid"
WORKER_PID_FILE="$RUN_DIR/worker.pid"
VISUAL_PID_FILE="$RUN_DIR/visual.pid"
#: 起几个 worker（D178）。SMB 多 worker 用 `PKH_WORKERS=4` 提升摄取吞吐；
#: 默认 1。⚠️ 多 worker 时配 `VISUAL_WRITE_VIA_SERVICE=true`，否则每个 worker
#: 各自加载 colSmol（2.65GB）—— 4 个就是 +10.6GB。
WORKERS="${PKH_WORKERS:-1}"
mkdir -p "$RUN_DIR"

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

WORKER_PATTERN='services\.worker\.main'
VISUAL_PATTERN='services\.visual_service\.main'

# 列出除本 shell 之外、匹配模式的进程
_stale_workers() {
    local p
    for p in $(pgrep -f "$WORKER_PATTERN" 2>/dev/null); do
        [ "$p" = "$$" ] && continue
        [ "$p" = "$PPID" ] && continue
        echo "$p"
    done
}

# 🔑 同上，但模式可传。视觉服务是第三个进程，它有**同样的坑 #2**
#   （`kill -- -PID` 杀不干净 `uv run` 的子进程）——
#   旧进程会继续占着 2.65GB 并用旧代码应答，而它"看起来是活的"。
_stale_by_pattern() {
    local pattern="$1" p
    for p in $(pgrep -f "$pattern" 2>/dev/null); do
        [ "$p" = "$$" ] && continue
        [ "$p" = "$PPID" ] && continue
        echo "$p"
    done
}

stop_worker() {
    local pid pf p
    # 1) 所有 pid 文件里的 leader（worker #1 是 worker.pid，其余 worker.<i>.pid）
    for pf in "$RUN_DIR"/worker.pid "$RUN_DIR"/worker.*.pid; do
        [ -f "$pf" ] || continue
        pid=$(cat "$pf" 2>/dev/null || true)
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    done
    # 2) 按命令行特征扫一遍残留（含 uv run 派生的子进程）
    for p in $(_stale_workers); do
        kill "$p" 2>/dev/null && echo "停止 worker (pid $p)"
    done
    sleep 1
    # 3) 第二轮强杀第一轮没杀掉的
    for p in $(_stale_workers); do
        kill -9 "$p" 2>/dev/null && echo "强杀残留 worker (pid $p)"
    done
    rm -f "$RUN_DIR"/worker.pid "$RUN_DIR"/worker.*.pid
}

# 停视觉查询服务。⚠️ **它加载了 2.65GB 的 colSmol**，所以退出要等一等
# （torch 释放 + 进程退出的真实耗时实测 ~5s）—— 不等的话下一次 start
# 会撞上"端口还在 TIME_WAIT / 旧进程还没放内存"。
stop_visual() {
    local pid p
    pid=$(cat "$VISUAL_PID_FILE" 2>/dev/null || true)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
    for p in $(_stale_by_pattern "$VISUAL_PATTERN"); do
        kill "$p" 2>/dev/null && echo "停止视觉服务 (pid $p)"
    done
    sleep 2
    for p in $(_stale_by_pattern "$VISUAL_PATTERN"); do
        kill -9 "$p" 2>/dev/null && echo "强杀残留视觉服务 (pid $p)"
    done
    rm -f "$VISUAL_PID_FILE"
}

stop_api() {
    local pid p
    pid=$(cat "$API_PID_FILE" 2>/dev/null || true)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
    # uvicorn 也可能有残留（按端口兜底）
    for _ in $(seq 1 20); do
        ss -ltn 2>/dev/null | grep -q ":$PORT " || break
        sleep 1
    done
    p=$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | head -1)
    if [ -n "$p" ]; then
        kill "$p" 2>/dev/null && echo "停止 :$PORT 上的残留进程 (pid $p)"
        sleep 2
        p=$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | head -1)
        [ -n "$p" ] && kill -9 "$p" 2>/dev/null && echo "强杀 (pid $p)"
    fi
    rm -f "$API_PID_FILE"
}

# 🔑 视觉服务的就绪状况单独一段输出，且**明确它不是启动失败**：
#   视觉服务要 13s 加载 colSmol（实测 12.8~15.0s），而上面那个循环
#   等的是 API（2~4s 就绪）。所以"视觉服务还没好"是**正常时序**，
#   不是故障 —— 把两者混在一个失败分支里会让人以为部署坏了。
#
# ⚠️ 判据是 `/health` 的 `model_loaded` 而不是"端口通不通"：
#   端口通了只说明 uvicorn 起来了，模型没加载完时 `/encode` 仍是 503。
#   （`/search/debug` 那时会如实显示"模型未就绪"，并说清在等什么。）
_report_visual() {
    local n=0 vpid vbound
    for _ in $(seq 1 30); do
        if curl -s --noproxy '*' "http://127.0.0.1:$VISUAL_PORT/health" 2>/dev/null \
            | grep -q '"model_loaded":true'; then
            vbound=$(ss -ltnp 2>/dev/null | grep ":$VISUAL_PORT " \
                    | grep -oP 'pid=\K[0-9]+' | head -1)
            echo "视觉查询服务就绪 :$VISUAL_PORT（模型已加载，pid ${vbound:-?}）"
            return 0
        fi
        n=$((n + 1))
        [ "$n" -ge 3 ] && break     # 最多再等 6s；不等满 13s —— 视觉腿会降级，不阻塞部署
    done
    vpid=$(cat "$VISUAL_PID_FILE" 2>/dev/null || true)
    echo "⚠️ 视觉查询服务 :$VISUAL_PORT 尚未就绪（pid ${vpid:-?}，日志 /tmp/pkh_visual.log）"
    echo "   这一步**不影响检索**：视觉腿会降级成 skipped，文本三条腿照常返回。"
    echo "   首次加载 colSmol 实测 13s、占 2.65GB；不想装就 VDR_ENABLED=false / VISUAL_SERVICE_URL= 空。"
}

case "${1:-start}" in
    stop)
        stop_worker
        stop_visual
        stop_api
        ;;
    restart|start)
        [ "${1:-start}" = restart ] && { "$0" stop; sleep 2; }

        # ⚠️ 启动前确认没有残留 worker —— 见文件头坑 #3
        stale=$(_stale_workers | tr '\n' ' ')
        if [ -n "${stale// /}" ]; then
            echo "⚠️ 有残留 worker (pid: $stale)，先停掉"
            stop_worker
        fi

        setsid uv run uvicorn services.api.main:app \
            --host 127.0.0.1 --port "$PORT" \
            > /tmp/pkh_api.log 2>&1 < /dev/null &
        echo $! > "$API_PID_FILE"
        for _i in $(seq 1 "$WORKERS"); do
            _log=/tmp/pkh_worker.log
            _pidf="$WORKER_PID_FILE"
            if [ "$_i" -gt 1 ]; then
                _log="/tmp/pkh_worker.$_i.log"
                _pidf="$RUN_DIR/worker.$_i.pid"
            fi
            setsid uv run python -u -m services.worker.main \
                > "$_log" 2>&1 < /dev/null &
            echo $! > "$_pidf"
        done
        # 🔴 视觉查询服务（D158）。**--host 127.0.0.1**：它没有鉴权，
        #   绑 0.0.0.0 等于让任何人都能刷这台机器的 2.65GB 模型。
        setsid uv run uvicorn services.visual_service.main:app \
            --host 127.0.0.1 --port "$VISUAL_PORT" \
            > /tmp/pkh_visual.log 2>&1 < /dev/null &
        echo $! > "$VISUAL_PID_FILE"

        for _ in $(seq 1 45); do
            sleep 2
            if curl -s --noproxy '*' "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
                bound=$(ss -ltnp 2>/dev/null | grep ":$PORT " \
                        | grep -oP 'pid=\K[0-9]+' | head -1)
                echo "API 就绪 :$PORT (pid ${bound:-?})"
                # 数**逻辑** worker 数，不是进程数 ——
                # `uv run` 会派生一个 python 子进程，所以 1 个 worker = 2 个进程。
                # 按 venv 里的 python 路径数才是准确的（uv run 那个是 wrapper）。
                # 只数真正执行代码的那个进程 —— `uv run` wrapper 的 argv 里
                # 也含 "python -u -m services.worker.main"，会被一起数进来，
                # 把 1 个 worker 报成 2 个。
                n_worker=$(ps -eo args | grep -c "[.]venv/bin/python3 -u -m services.worker.main")
                echo "worker 数: $n_worker / 期望 $WORKERS $([ "$n_worker" = "$WORKERS" ] || echo '⚠️ 不等（>期望说明有残留会抢任务）')"
                grep -q "worker 启动" /tmp/pkh_worker.log 2>/dev/null \
                    && echo "worker 已就绪" || echo "worker 未就绪，查 /tmp/pkh_worker.log"
                _report_visual
                exit 0
            fi
        done
        echo "启动超时："
        tail -8 /tmp/pkh_api.log /tmp/pkh_worker.log
        exit 1
        ;;
esac
