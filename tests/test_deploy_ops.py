"""🔑 备份与巡检的可测部分（阶段 7）。

这些脚本**不能只靠手动跑一次**来信任：

| 测什么 | 为什么单测验得到 |
|---|---|
| manifest 的行数口径是**活文档** | 与 `handle_reindex` 一致（§5.57）：软删未 purge 的不计入 |
| `verify` 真能抓到损坏 | 手测只验过"好的那一份"，抓不到时才发现判据是摆设 |
| 巡检三态（通过/失败/**跳过**） | 「跳过 ≠ 健康」是这轮修掉的真 bug |
| df 口径 | 拿"全部 chunk"当判据 → 回收站有东西就恒红（反向坑） |

⚠️ **不测**的部分：真的 `pg_dump` / `pg_restore` / `tar`（需要 Docker，
留给 `deploy/backup.sh` + `deploy/restore.sh` 的真实演练）。
这里只测"判据本身对不对"。
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# ============================================================
# 1. 脚本存在且语法正确（改坏了要立刻红，别等部署时）
# ============================================================


@pytest.mark.parametrize(
    "script",
    ["deploy/backup.sh", "deploy/restore.sh", "deploy/_backup_lib.sh"],
)
def test_shell_script_parses(script):
    """bash -n：语法错在**运行前**就能发现，不用等真跑一次。"""
    r = subprocess.run(["bash", "-n", str(ROOT / script)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"{script} 语法错：{r.stderr[:200]}"


def test_monitor_py_compiles():
    r = subprocess.run(
        ["python3", "-m", "py_compile", str(ROOT / "deploy" / "monitor.py")],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr[:200]


# ============================================================
# 2. `_backup_lib.sh` 里的关键判据
# ============================================================


def _shell_func(script: str, func: str) -> str:
    """取 shell 文件里某个函数的**代码**（去注释），从 `func() {` 到下一个同缩进收尾。

    锚点用函数名而不是中文小标题 —— 小标题会被改，函数名不会。
    """
    src = (ROOT / "deploy" / script).read_text(encoding="utf-8")
    i = src.index(func)
    # 往前找函数定义的行首
    start = src.rindex("\n", 0, i) + 1
    rest = src[i:]
    m = re.search(r"\n\}", rest)
    end = i + (m.end() if m else len(rest))
    return _code_only(src[start:end], lang="sh")


def _lib(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ROOT / "deploy" / "_backup_lib.sh"), *args],
        capture_output=True, text=True, cwd=ROOT,
    )


def test_table_counts_uses_exact_count_not_estimate():
    """🔑 判据必须是 `count(*)`，不是 `pg_stat_user_tables.n_live_tup`。

    估算值在 ANALYZE 没跑过时能差好几倍 —— 拿它当"备份对不对"的判据，
    就会永远"通过"或永远"失败"，两种都没意义。
    """
    body = _shell_func("_backup_lib.sh", "_pkl_table_counts()")
    assert "count(*)::bigint" in body
    assert "n_live_tup" not in body, (
        "行数统计不能用 pg_stat 的估算值 —— 判据必须是精确 count(*)"
    )


def test_manifest_and_verify_share_the_row_counter():
    """⚠️ manifest 与对账必须用**同一个**函数。

    两边各写一遍 SQL 的话，改了一边另一边不改 —— 症状是"恢复后少一张表的数据"，
    那是最坏的一类 bug（备份"成功"了，但内容不全）。
    """
    src = (ROOT / "deploy" / "_backup_lib.sh").read_text(encoding="utf-8")
    manifest_body = _shell_func("_backup_lib.sh", "_pkl_manifest()")
    assert "_pkl_table_counts" in manifest_body, "manifest 没有用统一的行数函数"
    # ⚠️ 逐表对账在 restore.sh 里**独立**写了 SQL（xpath 那个）：
    #   两边口径必须一样 —— "备份时记的行数"与"恢复时数的行数"要是不同算法，
    #   症状是"恢复后少一张表的数据"，那是最坏的 bug。
    r = (ROOT / "deploy" / "restore.sh").read_text(encoding="utf-8")
    assert "xpath('/row/c/text()'" in _code_only(r, lang="sh") or \
        "xpath(" in _code_only(r, lang="sh"), "restore 的对账 SQL 不见了"


def test_verify_checks_dump_is_not_empty():
    """🔑 `-Fc` 的**空库** dump 也会成功退出 —— 必须判"里面有没有 TABLE DATA"。

    只判"哈希对不对"的话，一份内容为空的备份会一路通过到恢复，
    而恢复完对账会发现"每张表都是 0 行"——那时才知道备份没内容。
    """
    src = _code_only((ROOT / "deploy" / "_backup_lib.sh").read_text(encoding="utf-8"),
                     lang="sh")
    assert "TABLE DATA" in src, (
        "verify 必须检查 dump 里有没有 TABLE DATA 条目（空 dump 会静默通过）"
    )


def test_qdrant_expect_uses_exact_count():
    """⚠️ 必须 `exact=true`：`indexed_vectors_count` 对新集合是 0。

    拿它当"重建目标"会让重建结果"对不上"，而其实是对的。
    """
    body = _shell_func("_backup_lib.sh", "_pkl_qdrant_expect()")
    assert '"exact":true' in body
    assert "indexed_vectors_count" not in body


def test_lib_does_not_dispatch_when_sourced():
    """🔑 `source` 进来时**不能**分发子命令。

    `backup.sh` / `restore.sh` 都 source 它，那时 `$1` 是调用者的参数
    （`--keep` / `--scratch`），直接分发会把它们当子命令 ——
    症状是备份脚本一启动就打印用法然后什么都不做（我真踩过）。
    """
    r = subprocess.run(
        ["bash", "-c",
         'source deploy/_backup_lib.sh --keep 3; echo "sourced-ok"'],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert "sourced-ok" in r.stdout, f"source 时不该分发：{r.stdout!r} {r.stderr!r}"
    assert "用法" not in r.stdout


# ============================================================
# 3. verify 的判据（造一份假备份，不碰 Docker）
# ============================================================


def _fake_backup(tmp_path: Path, *, tables: dict[str, int], blobs: int = 0) -> Path:
    d = tmp_path / "bk"
    d.mkdir()
    (d / "pg.dump").write_bytes(b"%PGDMP-not-a-real-dump\n")
    spec = {
        "format": 1,
        "created_at": "2026-10-04T00:00:00+08:00",
        "pg": {"file": "pg.dump", "sha256": "", "bytes": 0},
        "blobs": {"files": blobs},
        "table_rows": tables,
        "qdrant_expect": {},
    }
    import hashlib
    spec["pg"]["sha256"] = hashlib.sha256((d / "pg.dump").read_bytes()).hexdigest()
    (d / "manifest.json").write_text(json.dumps(spec, ensure_ascii=False))
    return d


def test_verify_rejects_missing_manifest(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    r = _lib("verify", str(d))
    assert r.returncode != 0 and "manifest" in (r.stderr + r.stdout)


def test_verify_rejects_wrong_dump_hash(tmp_path):
    """🔑 哈希不符必须**失败**（截断的 pg_dump 就是这种）。"""
    import hashlib

    d = _fake_backup(tmp_path, tables={"users": 3})
    spec = json.loads((d / "manifest.json").read_text())
    spec["pg"]["sha256"] = "0" * 64
    (d / "manifest.json").write_text(json.dumps(spec))
    r = _lib("verify", str(d))
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "哈希" in out or "不符" in out, out[:200]


def test_verify_flags_empty_dump_even_if_hash_matches(tmp_path):
    """🔑 哈希自洽但 dump 是空壳 → 也要失败。

    这是"哈希对 ≠ 备份可用"的具体形态：`-Fc` 空 dump 也有合法哈希。
    """
    d = _fake_backup(tmp_path, tables={"users": 0})
    r = _lib("verify", str(d))
    out = r.stdout + r.stderr
    # 假 dump 里没有 TABLE DATA，也没有可用表清单 → 必须红
    assert r.returncode != 0, out[:300]


def test_verify_reports_zero_row_tables(tmp_path):
    """备份时有空表**不算失败**（小库很正常），但要**说出来**。"""
    d = _fake_backup(tmp_path, tables={"users": 0, "jobs": 5})
    r = _lib("verify", str(d))
    out = r.stdout + r.stderr
    # 会因为假 dump 失败，但**空表提示**必须出现（那是另一条独立判据）
    assert "0 行" in out, out[:300]


# ============================================================
# 4. 巡检脚本：三态与口径
# ============================================================


def _monitor_import():
    """按路径加载 `deploy/monitor.py`（它不在包里）。

    ⚠️ **必须先注册进 `sys.modules`**：`dataclasses` 在定义 dataclass 时会做
    `sys.modules.get(cls.__module__).__dict__`，模块没注册就是
    `AttributeError: 'NoneType' object has no attribute '__dict__'` ——
    报错完全指不到"你忘了 sys.modules[name] = mod"。
    """
    import importlib.util
    import sys

    name = "pkh_monitor"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "deploy" / "monitor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # ← 缺这一行 dataclass 就炸
    spec.loader.exec_module(mod)
    return mod


def _py_func(sig: str, src: str | None = None) -> str:
    """取 Python 文件里某个函数的源码（到下一个顶层 `def`/`@`/`class` 为止）。

    @param src 源码；默认 `deploy/monitor.py`。D143 起有些判据搬到了
        `common/consistency.py`（唯一那份对账实现），需要指明文件。
    """
    src = src if src is not None else (ROOT / "deploy" / "monitor.py").read_text(
        encoding="utf-8"
    )
    i = src.index(sig)
    m = re.search(r"\n(?:@|def |class )", src[i + 1:])
    return src[i: i + 1 + (m.start() if m else len(src))] if m else src[i:]


def _code_only(text: str, *, lang: str) -> str:
    """只留**可执行代码**（去注释 / docstring）。

    🔑 用原文匹配去断言"代码里不该出现 X"时，会先撞上**解释这件事的注释**
    —— 本项目已经因此翻车两次（`except Exception` 那次、§5.57 的口径那次）。
    所以断言必须扫"去掉注释之后"的文本。
    """
    if lang == "py":
        # 去掉整行注释、docstring（模块级/函数级的三引号块）、行内注释
        text = re.sub(r'"""[\s\S]*?"""', "", text)
        out = []
        for line in text.splitlines():
            line = re.sub(r"\s+#.*$", "", line)
            if line.lstrip().startswith("#"):
                continue
            out.append(line)
        return "\n".join(out)
    # shell：去整行 # 与行尾 #
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


def test_check_ok_is_not_marked_failed():
    """🔑 渲染/分类必须先看 `ok`，再看 `level`。

    第一版按 `level` 取标记 → `ok=True, level="fail"`（磁盘还有余量）
    被画成 ✗，报告里全是红的而**没有一项真有问题**。
    """
    m = _monitor_import()
    c = m.Check("磁盘占用", True, "32% 已用", level="fail")
    r = m.Report()
    r.add(c)
    assert c not in r.failed, "通过的检查不该进 failed"
    assert "✗" not in m.render(r), "通过的检查不该渲染成 ✗"


def test_skipped_is_neither_ok_nor_failed():
    m = _monitor_import()
    r = m.Report()
    r.skip("索引对账", "没有注册过的 Key")
    assert r.skips and not r.failed
    assert "?" in m.render(r), "跳过的检查要用 ? 而不是 ✓/✗"


def test_skipped_makes_report_unhealthy():
    """🔑 **跳过不算健康**：巡检一半没跑成，不能报"一切正常"。

    第一版只判 failed/warned → 两项鉴权检查被跳过时，
    报告结尾仍写"一切正常"、退出码 0，cron 每天"绿"着而
    索引一致性从没被查过。这是监控工具最坏的失败：它让人放心。
    """
    m = _monitor_import()
    r = m.Report()
    r.add(m.Check("Postgres 可连", True, "正常"))
    r.skip("索引对账", "没有 Key")
    assert r.healthy is False
    assert r.as_dict()["counts"]["skipped"] == 1


def test_warned_makes_report_unhealthy():
    m = _monitor_import()
    r = m.Report()
    r.add(m.Check("磁盘", False, "91%", level="warn"))
    assert r.healthy is False


def test_monitor_df_uses_total_chunks():
    """🔑 df 判据必须是**全部 chunk 行**（含软删未 purge 的，D115）。

    ⚠️ 这条断言**改过一次**：我先前把它写成"必须是活文档 chunk 数"，
    理由是"`handle_reindex` 只登记活文档"—— 而那条理由本身就说明
    **入库路径与 reindex 口径不一致**（入库登记全部、软删不清 df）。
    两条路径口径不同 → **任何不变量都不成立**（monitor 恒红）。
    ✅ 统一到 purge 生命周期：reindex 也登记全部 chunk，向量仍只重建活的。

    ⚠️ 也**不要**用"活文档 chunk 数"：那是**向量侧**口径
    （`admin.py::_pg_point_ids` 也按活文档算），套到词表上必然恒红。
    """
    body = _code_only(_py_func("def check_df"), lang="py")
    assert 'psql("SELECT count(*) FROM chunks")' in body.replace(
        "\n", " ").replace("    ", " ") or "SELECT count(*) FROM chunks" in body
    # 🔑 不能按 is_current 过滤（那是向量侧口径）
    assert "is_current IS TRUE" not in body, (
        "df 检查用了『活文档』口径 —— 那是向量侧口径，会恒红"
    )
    assert body.count("count(*) FROM chunks") == 1, "df 检查只该数一次 chunk"


def test_monitor_index_check_is_not_scoped_by_api_visibility():
    """🔑 索引对账**不能**走 API（巡检账号看不到真实内容）。

    `/admin/consistency` 的范围是 `ctx.readable_kb_ids`，而巡检账号
    永远加不进别人的工作区（403 KB_NOT_FOUND，fail-closed 正确）——
    于是它只看得到公共库、且那两个恰好是空的，**报"各腿一致"**。
    这是监控工具最坏的失败：**检查"通过"，但什么都没查**。

    ✅ 所以巡检**直接**比 PG 与 Qdrant，不经 HTTP。
    🔑 D143：算术搬进 `services/common/consistency.py`（端点 / 巡检 / worker
    的定期对账共用**一份**），复用的是那里的 `qdrant_point_ids`，
    而它内部用的就是 `common.qdrant.point_id`（点 ID 编码只能有一份实现）。
    ⚠️ 下面三条断言分两层：**巡检不许走 API**（本条的性质）、
    **取点与翻页必须在那份唯一实现里**（跟着代码搬了家）。
    """
    body = _code_only(_py_func("def check_index"), lang="py")
    assert "admin/consistency" not in body, (
        "对账还在走 API —— 范围会被巡检账号的可见性限死"
    )
    # 🔑 巡检经 `_leg_drift()` 取数（两个检查项共用一次计算，带缓存）
    assert "_leg_drift" in body, "巡检没有走共享的对账计算"
    helper = _code_only(_py_func("def _all_legs"), lang="py")
    assert "check_drift" in helper, "巡检没有调用共享实现的 check_drift"

    # 取点与翻页在那份唯一实现里
    shared = (ROOT / "services" / "common" / "consistency.py").read_text(
        encoding="utf-8"
    )
    assert "from .qdrant import get_client, point_id, point_id_for_page" in shared, (
        "共享实现必须复用 common.qdrant 的 point_id —— "
        "两份 ID 编码会永远对不上"
    )
    scroll_fn = _code_only(_py_func("def qdrant_point_ids", src=shared), lang="py")
    assert "scroll" in scroll_fn, "对账没直接读 Qdrant"
    assert "next_page_offset" in scroll_fn, "Qdrant scroll 必须翻页（默认只回 10 条）"


def test_monitor_stuck_job_uses_locked_at():
    """⚠️ `jobs` 表**没有** `started_at`；`psql` 查不存在的列只报错、stdout 为空。

    于是判据变成"查不了"，看起来像"没有卡住的任务"（实际是没查）——
    这种假绿比红更坏。`locked_at` 才是 worker 领走任务的时刻；
    `updated_at` 也不行（进度回调会不停更新它）。
    """
    body = _code_only(_py_func("def check_jobs"), lang="py")
    assert "locked_at" in body
    assert "started_at" not in body
    assert "updated_at" not in body


# ============================================================
# 4b. 🔑 R4/D176：可观测性两项（失败积压 / 队列滞后）
# ============================================================


def test_monitor_registers_observability_checks():
    """🔑 巡检真的注册了那两项（文章 `dlq_size` / `queue_to_index_lag` 的代理）。"""
    src = (ROOT / "deploy" / "monitor.py").read_text(encoding="utf-8")
    assert '("failed", check_failed_jobs)' in src, "失败积压项没进 CHECKS"
    assert '("lag", check_queue_lag)' in src, "队列滞后项没进 CHECKS"
    # D191：索引与查询的 Embedding 模型必须一致（文章硬规则）
    assert '("embed", check_embedding_model)' in src, "Embedding 一致性项没进 CHECKS"


def test_monitor_queue_lag_uses_created_at_not_locked_at():
    """⚠️ 队列滞后 = "任务还没被领走多久了" ⇒ 判据是 `created_at`。

    `locked_at` 是 worker 领走时才写的（那是 `check_jobs` 的判据）；
    用错列会把"正在跑的长任务"也算成积压。
    """
    body = _code_only(_py_func("def check_queue_lag"), lang="py")
    assert "created_at" in body
    assert "locked_at" not in body, "队列滞后看错了列（那是卡住任务的判据）"
    assert "status='pending'" in body


def test_monitor_failed_jobs_uses_a_recent_window():
    """⚠️ 失败积压必须看**近 24h**：累计失败只增不减，会变成一个永远红的项。"""
    body = _code_only(_py_func("def check_failed_jobs"), lang="py")
    assert "status='failed'" in body
    assert "24 hours" in body, "失败积压没有时间窗 ⇒ 长期红的项等于没有项"


def test_monitor_qdrant_health_tolerates_plain_text():
    """🔑 `/healthz` 回**纯文本**，不能拿 json.loads 去解析。

    解析失败 → "Qdrant 活着"恒红，而它明明活着（我真踩过：
    `JSONDecodeError: Expecting value: line 1 column 1`）。
    """
    m = _monitor_import()
    assert m.http_text("http://127.0.0.1:1/nope")[0] == 0  # 连不上也不抛
    body = _code_only(_py_func("def check_qdrant"), lang="py")
    assert "/healthz" in body and "http_text" in body
    assert "http_json(f\"{QDRANT}/healthz\"" not in body


def test_monitor_skips_db_queries_when_only_other_checks():
    """`--only` 要真的只跑指定的检查（否则 `--only df` 也会去连 Qdrant）。"""
    m = _monitor_import()
    r = m.run_report(["df"])
    names = [c.name for c in r.checks]
    assert all("Qdrant" not in n for n in names), names

# ============================================================
# 5. 🔑 reindex 的口径与孤儿点清理（D115 · 本轮真实发现）
# ============================================================


def _handlers_src() -> str:
    return (ROOT / "services" / "worker" / "handlers" / "__init__.py").read_text(
        encoding="utf-8"
    )


def _handlers_func(name: str) -> str:
    """handlers 里某个函数的**代码**（去注释），到下一个顶层 `def`/`@` 为止。"""
    src = _code_only(_handlers_src(), lang="py")
    i = src.index(name)
    m = re.search(r"\n(?:@|def )", src[i + 1:])
    return src[i: i + 1 + m.start()] if m else src[i:]


def test_reindex_registers_all_chunks_not_only_live():
    """🔑 重建词表必须登记**全部** chunk（含软删未 purge 的）。

    第一版按 `is_current` 过滤，于是它与**入库路径**口径不同：
    入库登记全部、软删不清 df（D26 的 30 天窗口），
    而 reindex 只登记活的 → 回收站里有一份文档时 `n_docs` 与 chunk 行数
    对不上（实测差 8），**任何不变量都不成立**。

    ⚠️ 判据用 AST 而不是源码文本：曾经写死 `"todo = [d for d in live_docs"`，
    而 D124 把那个变量改名成 `live_rows`（顺带修掉了惰性关系的时间窗），
    测试就红了 —— 但**意图并没有变**（向量重建仍然只用活文档）。
    文本匹配会把"改名"报成"回归"，那种红会让人下一次直接改测试。
    """
    body = _handlers_func("def handle_reindex(")
    # 词表段用 select(Document)（全部），向量段用活文档
    assert "select(Document))" in body.replace("\n", " ").replace("  ", " "), (
        "词表重建应遍历**全部** Document（与入库路径同口径）"
    )

    tree = ast.parse(body)
    todo = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "todo" for t in n.targets
        )
    ]
    assert todo, "找不到 todo 的赋值 —— 向量重建的候选集合变了？"
    src_of_todo = ast.unparse(todo[0].value)
    assert "all_docs" not in src_of_todo, (
        f"向量重建不能用 all_docs（软删文档的点会被 delete_by_filter 清掉）：{src_of_todo}"
    )
    assert "live_" in src_of_todo, (
        f"向量重建要用**活**文档集合：{src_of_todo}"
    )


def test_reindex_cleans_orphan_kb_points():
    """🔑 reindex 要清掉「库已不在 PG 里」的孤儿点（真实发现：每腿 19733 个）。

    eval 的清理用 `DELETE FROM users` **级联硬删**文档，而 Qdrant 的点
    只在软删/清理任务里被删 —— 硬删路径从不碰索引，于是那些点永远留着，
    `delete_by_filter(kb_id=…)` 也够不着（那个 kb_id 已不在 PG 里、
    不在 `targets` 里）。

    不清的后果不只是占内存：`deploy/monitor.py` 的对账项会一直报"多 19733"，
    而**一个长期红的监控项等于没有监控项**（人会开始忽略它）。
    """
    src = _code_only(_handlers_src(), lang="py")
    assert "_orphan_kb_ids_in_qdrant" in src, "reindex 没有清孤儿点"
    body = _handlers_func("def _orphan_kb_ids_in_qdrant(")
    # ⚠️ 只取 payload 里的 kb_id（全量 scroll 会让 reindex 慢一个数量级）
    assert '"with_payload": ["kb_id"]' in body.replace("'", '"') or (
        "with_payload" in body and "kb_id" in body
    ), "孤儿库识别应只拉 payload 的 kb_id 字段"
    assert "next_page_offset" in body, "scroll 必须翻页"


def test_orphan_cleanup_does_not_touch_live_kbs():
    """🔴 绝不能删掉**还活着**的库的点。

    `_orphan_kb_ids_in_qdrant(colls, live_kb_ids)` 的正确性全在
    "只在 Qdrant 里、且不在 live_kb_ids 里"这个条件上 ——
    写反了就是"重建一次索引，全站检索结果清零"。
    """
    body = _handlers_func("def _orphan_kb_ids_in_qdrant(")
    assert "not in live_kb_ids" in body, "孤儿判据必须是「不在 live_kb_ids 里」"
    assert "in live_kb_ids" not in body.replace("not in live_kb_ids", ""), (
        "只有「不在」这一个方向；出现正向包含会把活库的点判成孤儿"
    )
