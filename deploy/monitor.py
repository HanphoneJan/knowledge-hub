#!/usr/bin/env python3
"""🔑 运维巡检（阶段 7 · 监控）。

## 为什么要有这个

这个项目是**单机自部署**（D8）：没有 k8s、没有 APM、没有告警通道。
出了事只有两种发现方式：用户说"搜不到东西了"，或者自己想起来看一眼。
后者不会发生 —— 所以需要一条**能随手跑、且退出码有意义**的命令：

```bash
uv run python deploy/monitor.py            # 巡检（人看）
uv run python deploy/monitor.py --json     # 机读（给 cron / CI）
uv run python deploy/monitor.py --quiet    # 只在有问题时输出（cron 用）
```

`--quiet` + 退出码是**给 cron 的**：`0` 一切正常、`1` 有问题。
放一条 cron 每天早上跑一次，比"想起来看一眼"可靠得多。

## 🔑 巡检项为什么是这些

每条都对应一个**真实出现过或明确会发生的**故障，不是"标准检查项"抄来的：

| 检查 | 它抓的是 |
|---|---|
| Postgres 可连 + `pg_isready` | 库挂了 → 全站 500 |
| Qdrant `/healthz` + 可写 | 索引服务挂了 → 检索降级、摄取全失败 |
| API `/health` 的 `status` | 进程活着但依赖坏了（`/healthz` 只看进程） |
| **df 一致性**（PG chunk 数 vs BM25 `n_docs`） | §5.29：**不是报错，是检索质量悄悄变差** |
| **索引对账**（`GET /admin/consistency` 的 `legs`） | 分腿漂移（dense 对、sparse 缺）—— 真实故障模式 |
| **视觉腿对账**（`kb_visual`） | 第三条腿没人管（D124：`kb_visual` 烂了几个月没人知道） |
| **卡住的文档**（中间态却无待办任务） | D127：任务终态没传给 `documents.status` → 用户永远看到"处理中" |
| 磁盘占用 | blob + 数据库都在同一个分区；写满会让**摄取静默失败** |
| 卡住的任务 | `running` 超过阈值 → worker 卡了（D78 之外的第二种"看起来正常"） |
| **失败任务积压**（近 24h） | 有没有需要人看的失败（文章 `dlq_size`/`failed_updates_total` 的代理；D176） |
| **队列滞后**（最老 pending） | 任务堆着没人领（文章 `queue_to_index_lag`；D176） |

⚠️ **磁盘是"静默失败"的那一类**：写不进去时上传接口仍返回 202
（blob 写失败在 handler 里），用户看到的是"文档一直是 pending"。
同理，"卡住的文档"也是 D127 之前**全绿**的那一类：
巡检当时只查 `jobs.running` 卡没卡，而失败的任务是 `failed`、不是 `running`。

## 一致性检查的**口径**要写清楚

`n_docs`（BM25 语料统计）统计的是**活文档**的 chunk —— 软删但未 purge 的
不计入（`handle_reindex` 就这么算的，见 `tests/test_bm25_stats.py`）。
拿"全部 chunk 行"当判据会让这条检查在"回收站里有东西"时恒红。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 🔑 必须把项目根塞进 `sys.path`：`python deploy/monitor.py` 的 `sys.path[0]`
#   是 `deploy/` 而不是项目根，于是 `from services.common.qdrant import point_id`
#   报 `ModuleNotFoundError: No module named 'services'` ——
#   而症状是"索引对账这项检查自己崩了"，与被测系统毫无关系。
#   （`eval/_reset.py` 也是这么做的，两处保持一致。）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
API = os.environ.get("PKH_API_URL", "http://127.0.0.1:8010")
QDRANT = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
PG_CONTAINER = os.environ.get("PG_CONTAINER", "pkh-postgres")
PG_USER = os.environ.get("POSTGRES_USER", "pkh")
PG_DB = os.environ.get("POSTGRES_DB", "pkh")
PG_PASS = os.environ.get("POSTGRES_PASSWORD", "")

# 🔑 `trust_env=False` 的等价物：urlopen 会读 `http_proxy` 环境变量，
#   而这台机器上设了代理 → 访问 127.0.0.1 会被绕到代理上去（连接超时）。
#   本项目踩过同样的坑（eval 脚本里每处 httpx 都写了它）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 磁盘占用超过这个比例就报警（写满会让摄取静默失败）
DISK_WARN = 0.85
#: `running` 超过这个秒数算卡住（正常摄取最长几分钟）
STUCK_SECONDS = 1800
#: df 允许的偏差（BM25 统计量是整数、且可能在任务队列里短暂滞后）
DF_TOLERANCE = 0


@dataclass
class Check:
    """一项检查的结果。

    🔑 **三态**（不是两态）：`skipped` 是"这项没法验"，既不是通过也不是失败。
    最典型的就是"巡检自己没配 API Key" —— 把它算成失败，
    那运维每天看到一条红色却**与被测服务无关**，几次之后就没人看这条命令了；
    把它算成通过同样糟：它会让人以为鉴权类检查覆盖到了。
    """

    name: str
    ok: bool
    detail: str
    #: fail = 影响服务；warn = 需要留意；skip = 无法验证
    level: str = "fail"

    @property
    def skipped(self) -> bool:
        return self.level == "skip"


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, c: Check) -> None:
        self.checks.append(c)

    def skip(self, name: str, why: str) -> None:
        self.add(Check(name, True, why, level="skip"))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.level == "fail"]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.level == "warn"]

    @property
    def skips(self) -> list[Check]:
        return [c for c in self.checks if c.skipped]

    @property
    def healthy(self) -> bool:
        """🔑 **跳过不算健康**：巡检一半没跑成，不能报"一切正常"。

        第一版只判 failed/warned，于是两项鉴权检查被跳过时，
        报告结尾仍然写"一切正常"、退出码 0 ——
        cron 每天"绿"着，而索引一致性**从来没被查过**。
        这是监控工具最坏的失败：它让人放心。
        """
        return not self.failed and not self.warned and not self.skips

    def as_dict(self) -> dict:
        return {
            # ⚠️ `ok` 语义是"全查了且都好"。跳过的项另列 `skipped`，
            #    让调用方能区分"服务健康"与"巡检没配好"。
            "ok": self.healthy,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "counts": {
                "failed": len(self.failed),
                "warned": len(self.warned),
                "skipped": len(self.skips),
            },
            "checks": [
                {"name": c.name, "ok": c.ok, "level": c.level, "detail": c.detail}
                for c in self.checks
            ],
        }


def http_text(url: str, timeout: float = 5.0) -> tuple[int, str]:
    """只要状态码与文本。

    ⚠️ 健康端点**常返回纯文本**（Qdrant 的 `/healthz` 是
    `healthz check passed`，不是 JSON）—— 拿 `json.loads` 去解析它的结果是
    `JSONDecodeError: Expecting value`，于是"Qdrant 活着"永远红着，
    而它明明活着。
    """
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def http_json(url: str, token: str | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # ⚠️ 4xx/5xx 的**响应体**也要拿到：巡检报"HTTP 500"没有信息量，
        #    而错误体里有具体原因。
        body = e.read().decode("utf-8", "replace")[:200]
        return e.code, body
    except Exception as e:  # noqa: BLE001 — 巡检不能因一个检查崩掉
        return 0, f"{type(e).__name__}: {e}"


def _dotenv_get(key: str) -> str | None:
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip("'\"") or None
    return None


#: 缓存的验证结果（一次巡检里只验一次）
_KEY_STATE: dict[str, object] = {}


def api_key() -> tuple[str | None, str]:
    """`(可用的 Key, 说明)`。

    🔑 来源顺序：`PKH_MONITOR_KEY` 环境变量 → `.env` 的 `PKH_MONITOR_KEY`
       → `.env` 的 `PKH_API_KEY`。

    ⚠️ `.env` 的 `PKH_API_KEY` **常常不是注册过的 Key**（本地开发写的是一个
    约定值），拿它去调 `/health` 会 401。所以必须**验一次**再决定要不要报失败：
    "巡检没配好"要显示成"跳过"，不能显示成"服务不健康" ——
    否则这份报告会教人忽略红色。
    """
    for cand in (os.environ.get("PKH_MONITOR_KEY"), _dotenv_get("PKH_MONITOR_KEY"),
                 _dotenv_get("PKH_API_KEY"), os.environ.get("PKH_API_KEY")):
        if not cand:
            continue
        st, _ = http_json(f"{API}/api/v1/auth/me", cand, timeout=4.0)
        if st == 200:
            return cand, "已用注册过的 Key 验证"
        why = f"HTTP {st}"
    return None, (
        "没有**注册过**的 API Key：`.env` 里的 PKH_API_KEY 在库里不存在。"
        "配一个：注册后把返回的 api_key 写进 `.env` 的 `PKH_MONITOR_KEY`"
    )


def psql(sql: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["docker", "exec", "-e", f"PGPASSWORD={PG_PASS}", PG_CONTAINER,
             "psql", "-U", PG_USER, "-d", PG_DB, "-tAc", sql],
            capture_output=True, text=True, timeout=15,
        )
        return p.returncode, p.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


# ============================================================
# 各项检查
# ============================================================


def check_postgres(r: Report) -> None:
    rc, out = psql("SELECT 1")
    r.add(Check("Postgres 可连", rc == 0 and out == "1",
                "正常" if rc == 0 else out[:120]))


def check_qdrant(r: Report) -> None:
    # ⚠️ 用 http_text：`/healthz` 回的是纯文本 `healthz check passed`，
    #   拿 json.loads 解析它会抛 JSONDecodeError → "Qdrant 活着"恒红。
    st, body = http_text(f"{QDRANT}/healthz")
    ok = st == 200
    r.add(Check("Qdrant 活着", ok, (body or "正常") if ok else body[:120]))

    # 可写性：只读的健康端点在"磁盘满/集合只读"时照样 200
    # ⚠️ 用 collection 列表代替真写一个点：真写会污染数据
    #    （写了还得删，删失败就留了个孤儿点）。
    st2, body2 = http_json(f"{QDRANT}/collections")
    cols = []
    if st2 == 200 and isinstance(body2, dict):
        cols = [c.get("name") for c in body2.get("result", {}).get("collections", [])]
    r.add(Check("Qdrant 可列集合（能读）", st2 == 200,
                ", ".join(c or "?" for c in cols) or f"HTTP {st2}"))


def check_api(r: Report) -> None:
    token, why = api_key()
    if not token:
        r.skip("API 健康", why)
        return
    st, body = http_json(f"{API}/api/v1/health", token)
    if st != 200:
        r.add(Check("API 健康", False, f"HTTP {st} {str(body)[:100]}"))
        return
    status = body.get("status") if isinstance(body, dict) else None
    deps = body.get("deps") or body.get("dependencies") or {}
    bad = [k for k, v in (deps.items() if isinstance(deps, dict) else [])
           if isinstance(v, dict) and v.get("ok") is False]
    r.add(Check("API 健康", status in (None, "ok", "healthy") and not bad,
                f"status={status}" + (f" 异常依赖={bad}" if bad else "")))


def check_df(r: Report) -> None:
    """BM25 语料统计 vs 活文档 chunk 数（§5.29 的静默漂移）。"""
    rc, n_docs = psql("SELECT n_docs FROM corpus_stats LIMIT 1")
    if rc != 0 or not n_docs.isdigit():
        r.add(Check("BM25 df 一致", False, f"读不到 corpus_stats：{n_docs[:100]}",
                    level="warn"))
        return
    # 🔑 口径 = **全部 chunk 行**（含软删未 purge 的），与 `handle_reindex`
    #   一致（D115）。⚠️ 不要用"活文档 chunk 数"：那是**向量侧**口径，
    #   回收站里有一份文档就差一截 → 每小时报一次假警，
    #   而每小时假警会让人开始忽略巡检（比不报更糟）。
    rc2, total = psql("SELECT count(*) FROM chunks")
    if rc2 != 0 or not total.isdigit():
        r.add(Check("BM25 df 一致", False, f"数不了 chunk：{total[:100]}", level="warn"))
        return
    a, b = int(n_docs), int(total)
    ok = abs(a - b) <= DF_TOLERANCE
    r.add(Check(
        "BM25 df 一致（检索质量）", ok,
        f"n_docs={a} chunk 行={b}" + ("" if ok else f" 差 {a - b}"),
        level="warn" if ok else "fail",
    ))


#: 对账结果缓存。⚠️ 两个检查项（文本腿 + 视觉腿）问的是**同一次**计算 ——
#:   不缓存的话一次巡检要 scroll 三条腿两遍，几万点时白等一倍时间。
#:   （以前是两个函数各算各的：pg 那份算一次、Qdrant 那份再算一次。）
_DRIFT_CACHE: dict | None = None

# 🔑 腿名从共享模块拿（不自己在monitor 里再写一份字符串常量）
from services.common.consistency import COLL_DENSE, COLL_SPARSE, COLL_VISUAL


def _all_legs() -> dict | None:
    """一次算完三条腿，缓存起来。读不到 PG/Qdrant 时返回 `None`。"""
    global _DRIFT_CACHE
    if _DRIFT_CACHE is None:
        from services.common.consistency import check_drift
        from services.common.db import sync_session_scope

        try:
            with sync_session_scope() as s:
                _DRIFT_CACHE = check_drift(s, None)
        except Exception:  # noqa: BLE001 —— 读不了就当"查不了"，由调用方报 warn
            return None
    return _DRIFT_CACHE


def _leg_drift(coll: str) -> dict | None:
    """某一条腿的对账结果。

    🔑 D143：判据全部来自 `services/common/consistency.py` —— 与
    `GET /admin/consistency`（端点）和 worker 的定期对账**同一份**。
    以前巡检自己写了一份（psql 拼点 ID + urllib 拉 scroll）⇒ 三处各算一遍，
    改一处就会出现"端点说一致、巡检说有漂移"，而对账工具自己不一致
    是最难查的一类问题（D140 刚为「两份清单」付过代价）。
    """
    legs = _all_legs()
    return None if legs is None else legs.get(coll)


def check_visual_index(r: Report) -> None:
    """视觉腿对账（kb_visual）：**页级**点 vs `documents.scan_pages`。

    背景：`kb_visual` 恒为 0 个点，而 8 个 `embed_visual` 任务全部 failed，
    没有任何一处报警：巡检只比 dense/sparse 两条**文本**腿。
    「0 个点」于是被读成「库里没有照片」，一路让两个里程碑
    （M4 扫描件视觉路径 / M6 photo 路由收益）被误判成缺数据。
    **没有被对账覆盖的腿等于不存在的腿。**

    ⚠️ `VDR_ENABLED=false` 时这一项**跳过**而不是报红 —— 关掉视觉索引是
    运维决定，不是故障；报红只会训练人忽略巡检（「长期红的项等于没有项」）。
    """
    from services.common.config import get_settings

    if not get_settings().vdr_enabled:
        r.skip("视觉腿对账（kb_visual）",
               "VDR_ENABLED=false：不建视觉索引，这一项不适用")
        return

    leg = _leg_drift(COLL_VISUAL)
    if leg is None:
        r.add(Check("视觉腿对账（kb_visual）", False, "读不了 PG 或 Qdrant",
                    level="warn"))
        return
    ok = leg["missing"] == 0 and leg["dangling"] == 0
    r.add(Check(
        "视觉腿对账（kb_visual）", ok,
        (f"一致 · {leg['actual']} 个页级点") if ok
        else f"缺 {leg['missing']} / 多 {leg['dangling']}"
             f"（PG 说该有 {leg['expected']}，Qdrant 有 {leg['actual']}）",
        level="warn" if ok else "fail",
    ))


def check_index(r: Report) -> None:
    """索引对账：**直接**比 PG 与 Qdrant，分腿（dense / sparse）。

    🔑 **不走 API**：巡检账号无法加入别人的工作区（403），走 `/admin/consistency`
    只能看到自己的库 —— 而巡检回答的是"索引和库对不对得上"，
    不是"某个用户能看到什么"。
    ⚠️ 判据来自 `common/consistency.py`（D143），**不再自己算一遍**：
      那份"直接比 PG"的理由仍然成立（不走 API），但**算术**该由唯一的实现给出。

    ⚠️ PG 侧只算 `is_current AND deleted_at IS NULL` 的 chunk：
    软删文档**本来就不该**在索引里（先删 PG，异步清 Qdrant），
    把它们算进来会把「正在清理」报成「漏索引」，然后有人去重建，
    而重建又会把清理推翻一遍。
    """
    legs = [(_leg_drift(COLL_DENSE), COLL_DENSE), (_leg_drift(COLL_SPARSE), COLL_SPARSE)]
    if any(leg is None for leg, _ in legs):
        r.add(Check("索引对账（PG ↔ Qdrant）", False, "读不了 PG 或 Qdrant",
                    level="warn"))
        return

    bad: list[str] = []
    counts: list[str] = []
    for leg, coll in legs:
        miss, extra = leg["missing"], leg["dangling"]
        counts.append(f"{coll}:{leg['actual']}点")
        if miss or extra:
            bad.append(f"{coll} 缺{miss}/多{extra}")
    expect = max((leg["expected"] for leg, _ in legs if leg), default=0)
    r.add(Check(
        "索引对账（PG ↔ Qdrant）", not bad,
        (f"各腿一致 · PG {expect} 点 vs " + " ".join(counts)) if not bad
        else "；".join(bad) + f"（PG {expect} 点）",
        level="warn",
    ))



def check_disk(r: Report) -> None:
    """⚠️ 这一项是"静默失败"的那一类：写不进去时上传仍返回 202。"""
    try:
        u = shutil.disk_usage(ROOT / "data")
    except OSError as e:
        r.add(Check("磁盘占用", False, f"量不了：{e}", level="warn"))
        return
    pct = u.used / u.total
    lvl = "fail" if pct >= DISK_WARN else "warn"
    r.add(Check(
        "磁盘占用（写满会让摄取静默失败）", pct < DISK_WARN,
        f"{pct:.1%} 已用（{u.used >> 30}G/{u.total >> 30}G）",
        level=lvl,
    ))


def check_jobs(r: Report) -> None:
    """卡住的 `running` 任务：worker 卡了但没人知道。

    ⚠️ 时间列是 **`locked_at`**（worker 领走任务时写的），不是 `started_at` ——
    `jobs` 表里没有后者，而 psql **不存在的列只报错、stdout 为空**，
    于是判据变成"查不了"，看起来像"没有卡住的任务"（实际是没查）。
    拿 `updated_at` 也不对：进度回调会不停更新它，卡住的任务反而"很新"。
    """
    rc, out = psql(
        "SELECT count(*) FROM jobs WHERE status='running' "
        "AND locked_at IS NOT NULL "
        f"AND locked_at < now() - interval '{STUCK_SECONDS} seconds'"
    )
    n = int(out) if out.isdigit() else -1
    if n < 0:
        r.add(Check("没有卡住的任务", False, f"查不了：{out[:100]}", level="warn"))
        return
    r.add(Check(f"没有卡住的任务（running >{STUCK_SECONDS // 60} 分钟）", n == 0,
                "正常" if n == 0 else f"{n} 个任务卡住（worker 卡了？）",
                level="warn"))


#: 队列滞后（最老的 pending 任务等了多久）的红线。
#: 文章（rag-knowledge-update）把它叫 `queue_to_index_lag`；本项目的
#: "入队到开始处理"就是它。
#: ⚠️ 门槛取 30 分钟而不是文章的 5 分钟：本项目有 15s/页的视觉任务，
#:   大规模导入时 pending 排长队是**正常**的（D94：导入会堆上千个 ingest）——
#:   5 分钟会天天报假警，而"长期红的项等于没有项"（check_visual_index 的原话）。
QUEUE_LAG_WARN_SECONDS = 1800


def check_failed_jobs(r: Report) -> None:
    """近 24h 失败的更新任务（文章 `failed_updates_total` / `dlq_size` 的本项目代理）。

    🔑 本项目**没有独立死信队列**：失败停在 `jobs.status='failed'`
    （D176 之后永久错误**第一次尝试**即终态）。所以"有没有需要人看的失败"
    就由这张表回答。
    ⚠️ 口径 = **近 24h 失败**（按 `updated_at`），与 `/health` 的 `failed_24h`
    一致（D133）—— 累计失败会随历史只增不减，拿它当判据会变成一个**永远红**
    的项，而长期红的项等于没有项。
    """
    rc, out = psql(
        "SELECT count(*) FROM jobs WHERE status='failed' "
        "AND updated_at >= now() - interval '24 hours'"
    )
    n = int(out) if out.isdigit() else -1
    if n < 0:
        r.add(Check("失败任务积压", False, f"查不了：{out[:100]}", level="warn"))
        return
    r.add(Check(
        "失败任务积压（近 24h）", n == 0,
        "正常" if n == 0 else (
            f"{n} 个任务失败：查 `GET /api/v1/jobs?status=failed`；"
            "文档级失败可 `POST /documents/{id}/retry`"
        ),
        level="warn",
    ))


def check_queue_lag(r: Report) -> None:
    """最老的 pending 任务等了多久（文章 `queue_to_index_lag`）。

    ⚠️ 判据用 `created_at`（入队时刻），**不是** `locked_at`（worker 领走时才有）
    —— 这里要的正是"还没被领走多久了"（`check_jobs` 用的才是 `locked_at`）。
    """
    rc, out = psql(
        "SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0)::int "
        "FROM jobs WHERE status='pending'"
    )
    if rc != 0 or not out.lstrip("-").isdigit():
        r.add(Check("队列滞后", False, f"查不了：{out[:100]}", level="warn"))
        return
    lag = max(0, int(out))
    ok = lag <= QUEUE_LAG_WARN_SECONDS
    r.add(Check(
        f"队列滞后（最老 pending ≤{QUEUE_LAG_WARN_SECONDS // 60} 分钟）", ok,
        "无积压" if lag == 0 else (
            f"最老的待处理任务已等 {lag // 60} 分钟"
            + ("" if ok else "（worker 不够用？加 worker，或看 /health 的 queue 段）")
        ),
        level="warn",
    ))


def check_backup(r: Report, backup_dir: Path | None) -> None:
    """🔑 最近一份备份有多新、太旧了就是**事实上没有备份**。"""
    d = backup_dir or (ROOT / "data" / "backups")
    if not d.exists():
        r.add(Check("最近有备份", False, f"{d} 不存在（还没备份过）", level="warn"))
        return
    dirs = sorted([p for p in d.iterdir() if p.is_dir()])
    if not dirs:
        r.add(Check("最近有备份", False, f"{d} 里没有备份", level="warn"))
        return
    newest = dirs[-1]
    age_h = (time.time() - newest.stat().st_mtime) / 3600
    ok = age_h <= 24 * 3        # 3 天
    r.add(Check("最近有备份（≤3 天）", ok,
                f"{newest.name}（{age_h:.1f} 小时前）", level="warn"))


def check_embedding_model(r: Report) -> None:
    """🔑 D191：索引与查询必须用**同一个** Embedding 模型（文章硬规则）。

    不一致时 dense 腿会在两个不同的向量空间里算相似度 —— 排序基础失效，
    而且**不报错**（最坏的失败模式）。判据来自 `common/index_models.py`
    （写记录的是入库 / 重建那条路径，这里只读）。

    ⚠️ **空表 = 未知** ⇒ 跳过而不是报红：还没记录不等于不一致，
      而"长期红的项等于没有项"（check_visual_index 的原话）。
    """
    from services.common.config import get_settings
    from services.common.db import sync_session_scope
    from services.common.index_models import get_models, model_mismatch
    from services.common.qdrant import COLL_DENSE, COLL_VISUAL, VDR_DIM

    s = get_settings()
    try:
        with sync_session_scope() as ses:
            recorded = get_models(ses)
    except Exception as e:  # noqa: BLE001
        r.add(Check("Embedding 模型一致", False,
                    f"查不了 index_models：{type(e).__name__}", level="warn"))
        return

    if not recorded:
        r.skip("Embedding 模型一致（索引 ↔ 查询）",
               "还没有记录（新装 / 尚未重新摄取或重建）—— 入库后会写进 index_models")
        return

    problems = [
        reason
        for coll, model, dim in (
            (COLL_DENSE, s.text_embed_model, s.text_embed_dim),
            (COLL_VISUAL, s.vdr_model, VDR_DIM),
        )
        if (reason := model_mismatch(recorded.get(coll), model, dim))
    ]
    r.add(Check(
        "Embedding 模型一致（索引 ↔ 查询）", not problems,
        "一致" if not problems else "；".join(problems),
        level="warn" if not problems else "fail",
    ))


def check_stuck_docs(r: Report) -> None:
    """🔑 D127：文档停在"处理中"，却**没有任何待办任务**。

    背景：`documents.status` 与 `jobs.status` 是两份状态，而 D127 之前
    **没有任何代码**把后者搬给前者 —— 于是任务失败时文档永远停在
    `pending`/`parsing`，用户看到的是"一直在处理中"。
    而"没有待办任务却还在中间态"是**不可能**的正常状态：
    `pending` 是一个承诺（PROGRESS §5.91），谁入的队谁负责撤销它。

    ⚠️ 判据必须包含 `NOT EXISTS (未完成任务)`，否则 worker 一启动就会把
    **正在摄取**的几百个文档全报成卡住 —— 那是队列本身，不是故障。
    （单测 `tests/test_doc_state.py` 盯着这一条。）
    """
    rc, out = psql(
        "SELECT count(*) FROM documents d "
        "WHERE d.status IN ('pending','parsing','chunking','embedding') "
        "AND d.deleted_at IS NULL AND d.is_current IS TRUE "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.doc_id = d.id "
        "AND j.status IN ('pending','running'))"
    )
    if rc != 0 or not out.isdigit():
        r.add(Check("没有卡住的文档", False, f"查不了：{out[:100]}", level="warn"))
        return
    n = int(out)
    r.add(Check(
        "没有卡住的文档（中间态却无待办任务）", n == 0,
        "正常" if n == 0 else (
            f"{n} 份永远停在处理中：重启 worker 会自动补扫结算，"
            "否则对这几份调 POST /documents/{id}/reparse"
        ),
        # ⚠️ warn 而不是 fail：一份卡住不影响服务，但用户看得见。
        #   反过来，"长期红的项等于没有项"（见 check_visual_index 的说明）。
        level="warn",
    ))


CHECKS = (
    ("postgres", check_postgres),
    ("qdrant", check_qdrant),
    ("api", check_api),
    ("df", check_df),
    # 🔑 D191：索引与查询的 Embedding 模型必须一致（文章硬规则）——
    #   不一致时 dense 腿静默返回错排序，没有任何别的项会报出来。
    ("embed", check_embedding_model),
    ("index", check_index),
    # 🔑 D124：视觉腿（`kb_visual`）此前**完全没有**对账 ——
    #   它坏了没人知道，"0 个点"被读成"库里没照片"（见 check_visual_index）。
    ("visual", check_visual_index),
    # 🔑 D127：文档卡在"处理中"却没有任何待办任务（= 任务终态没传给文档）
    ("docs", check_stuck_docs),
    ("disk", check_disk),
    ("jobs", check_jobs),
    # 🔑 R4/D176：可观测性补两项（文章 dlq_size / queue_to_index_lag 的本项目代理）
    ("failed", check_failed_jobs),
    ("lag", check_queue_lag),
)


def run_report(only: list[str] | None = None) -> Report:
    r = Report()
    for name, fn in CHECKS:
        if only and name not in only:
            continue
        try:
            fn(r)
        except Exception as e:  # noqa: BLE001 — 一个检查崩了不该让整次巡检崩
            r.add(Check(f"检查 {name} 自身出错", False,
                        f"{type(e).__name__}: {e}"))
    try:
        check_backup(r, None)
    except Exception as e:  # noqa: BLE001
        r.add(Check("备份检查自身出错", False, f"{type(e).__name__}: {e}"))
    return r


def render(r: Report) -> str:
    lines = []
    for c in r.checks:
        # 🔑 skip 用「?」而不是 ✓/✗ —— 视觉上必须一眼看出"没验成"
        #   （用 ✓ 会让人以为查过了且没问题）。
        # ⚠️ 顺序必须是 skipped → ok → warn → fail：
        #   第一版先按 `level` 取标记、只看 level 不看 ok，
        #   于是 `ok=True, level="fail"`（磁盘还有余量）被画成 ✗ ——
        #   报告里全是红的，而**没有一项真有问题**。
        if c.skipped:
            mark = "\033[36m?\033[0m"
        elif c.ok:
            mark = "\033[32m✓\033[0m"
        elif c.level == "warn":
            mark = "\033[33m!\033[0m"
        else:
            mark = "\033[31m✗\033[0m"
        lines.append(f"{mark} {c.name:34} {c.detail}")
    if r.healthy:
        lines.append("\n\033[32m一切正常\033[0m")
    else:
        for c in r.failed:
            lines.append(f"\033[31m✗ {c.name}：{c.detail}\033[0m")
        for c in r.warned:
            lines.append(f"\033[33m! {c.name}：{c.detail}\033[0m")
        for c in r.skips:
            lines.append(f"\033[36m? {c.name}（跳过）：{c.detail}\033[0m")
        lines.append(
            f"\n\033[1m{len(r.failed)} 项影响服务 / {len(r.warned)} 项需要留意"
            f" / {len(r.skips)} 项跳过\033[0m")
    return "\n".join(lines)


def skipped_help(r: Report) -> list[str]:
    """跳过项的修复建议（否则用户只看到"跳过"，不知道要做什么）。"""
    return [
        f"{c.name}：{c.detail}"
        for c in r.skips
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="个人知识库运维巡检（阶段 7）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给 cron/CI）")
    ap.add_argument("--quiet", action="store_true", help="只在有问题时输出")
    ap.add_argument("--only", nargs="*", help=f"只跑这些：{' '.join(n for n, _ in CHECKS)}")
    args = ap.parse_args()

    r = run_report(args.only)
    if args.json:
        print(json.dumps(r.as_dict(), ensure_ascii=False, indent=2))
    elif not (args.quiet and r.healthy):
        print(render(r))
    # 🔑 退出码有意义才有"报警"可言：0 正常，1 有问题
    return 0 if r.healthy else 1


if __name__ == "__main__":
    sys.exit(main())