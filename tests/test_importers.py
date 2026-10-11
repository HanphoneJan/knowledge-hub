"""🔑 导入链的单测（`services/ingest/importers.py` + 两个 handler 的注册）。

按 CONVENTIONS §9 的原则：**覆盖"不该发生的事"**，每条对应一种真实故障模式：

| 测试 | 对应的坏情况 |
|---|---|
| `resolve_import_path` 白名单 | 租户传 `/etc` 或 `../` 读到别人的数据（D92） |
| `is_relative_to` 而不是 `startswith` | `data/imports_evil/` 被当成在白名单里 |
| `plan_chat_slices` 不丢消息 | 切分边界错 → 静默少导消息（D41） |
| `render_chat_slice` 时间戳进正文 | "那天我们聊了什么" 搜不到（D71 的失败模式） |
| uid 不进正文 | 对话方的账号标识（PII）写进可检索文本 |
| `count_messages` 分块边界 | 按 1MB 分块数换行符会多算/少算 |
| `scan_dir` 排序 | 两次导入顺序不同，diff 大到没法看 |
| HANDLERS 覆盖全部 kind | 任务入队了但永远跑不了（历史真实缺口） |
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.common.jobs import ALL_KINDS
from services.ingest import importers
from services.ingest.importers import (
    count_messages,
    import_roots,
    is_malformed,
    iter_messages,
    plan_chat_slices,
    render_chat_slice,
    resolve_import_path,
    scan_dir,
)
from services.ingest.parsers import IMAGE_SUFFIXES


# ============================================================
# 1. 路径白名单（D92）—— 安全红线
# ============================================================


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """造两个白名单根目录，并让配置只认它们。"""
    a = tmp_path / "imports"
    b = tmp_path / "imports_more"
    for d in (a, b):
        d.mkdir()
    monkeypatch.setattr(
        importers, "import_roots", lambda: [a.resolve(), b.resolve()]
    )
    return a


def test_accepts_file_inside_root(roots):
    f = roots / "chat.jsonl"
    f.write_text("{}\n")
    assert resolve_import_path(str(f), expect="file") == f.resolve()


def test_rejects_outside_root(roots, tmp_path):
    """"🔑 不在白名单内的路径一律拒绝 —— 这是跨租户数据泄漏的闸门。"""
    outside = tmp_path / "other_tenant" / "secret.jsonl"
    outside.parent.mkdir()
    outside.write_text("{}\n")
    with pytest.raises(Exception) as ei:
        resolve_import_path(str(outside))
    assert "IMPORT_ROOTS" in str(ei.value), "报错要说清配置项在哪"


def test_rejects_traversal_out_of_root(roots, tmp_path):
    """`../../` 形式的相对路径不能绕过白名单。

    ⚠️ 判据必须落在 `resolve()` **之后**：先判前缀再 resolve 会被绕过。
    """
    evil = roots / ".." / ".." / "etc" / "passwd"
    with pytest.raises(Exception):
        resolve_import_path(str(evil))
    # 相对路径也要挡住
    with pytest.raises(Exception):
        resolve_import_path("../../etc/passwd")


def test_sibling_prefix_is_not_inside(roots, tmp_path):
    """🔑 `data/imports_evil/` **不是** `data/imports/` 的子目录。

    用 `str.startswith` 判前缀就会放行 —— 那是最容易写出来的越权漏洞。
    """
    sibling = tmp_path / "imports_evil"
    sibling.mkdir()
    (sibling / "x.jsonl").write_text("{}\n")
    with pytest.raises(Exception):
        resolve_import_path(str(sibling / "x.jsonl"))


def test_symlink_escape_is_blocked(roots, tmp_path):
    """符号链接指向白名单外 → 拒绝（`resolve()` 会展开它）。"""
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n")
    link = roots / "link.jsonl"
    try:
        link.symlink_to(outside)
    except OSError:                      # 某些环境不允许建链接
        pytest.skip("本环境不支持符号链接")
    with pytest.raises(Exception):
        resolve_import_path(str(link))


def test_empty_path_rejected(roots):
    with pytest.raises(Exception):
        resolve_import_path("")
    with pytest.raises(Exception):
        resolve_import_path("   ")


def test_type_mismatch_is_400_not_404(roots):
    """在白名单内但类型不对 → 400（请求写错了），不是 404（像是不存在）。"""
    d = roots / "sub"
    d.mkdir()
    with pytest.raises(Exception) as ei:
        resolve_import_path(str(d), expect="file")
    assert "不是文件" in str(ei.value)
    f = roots / "x.jsonl"
    f.write_text("{}\n")
    with pytest.raises(Exception) as ei:
        resolve_import_path(str(f), expect="dir")
    assert "不是目录" in str(ei.value)


def test_missing_file_reports_422_not_403():
    """🔑 不存在 → 422，且**先判白名单再判存在**。

    ⚠️ 不能用 `ResourceNotFound`（403 / KB_NOT_FOUND）：那个码是给
    "知识库/文档"的，拿它报"文件不存在"会让调用方以为自己没权限，
    去申请权限 —— 而真正的原因是路径写错了。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "imports"
        root.mkdir()
        import services.ingest.importers as im

        orig = im.import_roots
        im.import_roots = lambda: [root.resolve()]
        try:
            with pytest.raises(Exception) as ei:
                resolve_import_path(str(root / "nope.jsonl"))
            assert getattr(ei.value, "status_code", None) == 422, (
                f"应该是 422，实际 {getattr(ei.value, 'status_code', None)}"
            )
            assert getattr(ei.value, "code", "") != "KB_NOT_FOUND"
        finally:
            im.import_roots = orig


def test_default_root_is_inside_data_dir(monkeypatch):
    """默认白名单 = `<data_dir>/imports`，不是整个文件系统。"""
    import services.ingest.importers as im

    monkeypatch.setattr(im, "import_roots", im.import_roots)
    rs = import_roots()
    assert rs, "至少要有一个白名单根"
    assert all(r.is_absolute() for r in rs)
    assert any(str(r).endswith("imports") for r in rs), rs


# ============================================================
# 2. 聊天切分（D41）
# ============================================================


def _msg(i: int, name: str = "李四中宝贝") -> dict:
    return {"id": f"m{i}", "ts": 1721535902000 + i * 60000,
            "date": "2024-07-21", "uid": "u_EXAMPLEopenid0000000000",
            "name": name, "text": f"第{i}条", "type": "text"}


def test_split_loses_nothing():
    """🔑 切分**一条都不能少** —— 静默丢消息是这类代码最难发现的 bug。"""
    msgs = [_msg(i) for i in range(437)]
    slices = plan_chat_slices(msgs, 50)
    flat = [m for s in slices for m in s]
    assert len(flat) == 437
    assert [m["id"] for m in flat] == [m["id"] for m in msgs], "顺序也不能变"
    assert [len(s) for s in slices] == [50] * 8 + [37]


def test_split_exact_multiple_has_no_empty_tail():
    """⚠️ 尾部不能出现空切片 —— 空文档会进索引并被检索命中。"""
    slices = plan_chat_slices([_msg(i) for i in range(100)], 50)
    assert len(slices) == 2 and all(s for s in slices)


def test_split_per_doc_zero_is_rejected_by_one():
    """`messages_per_doc=0` 会让切片步长为 0 → 死循环。"""
    out = plan_chat_slices([_msg(i) for i in range(5)], 0)
    assert len(out) == 5 and all(len(s) == 1 for s in out)


def test_split_empty_input():
    assert plan_chat_slices([], 50) == []


# ============================================================
# 3. 渲染：什么必须进正文，什么必须不进
# ============================================================


def test_timestamp_and_speaker_are_in_the_text():
    """🔑 时间戳必须进正文 —— "那天我们聊了什么" 靠它检索。

    只放进文件名的话，正文检索完全找不到时间（§4 的失败模式之一）。
    """
    name, text = render_chat_slice([_msg(0)], 1)
    assert "2024-07-21" in text
    assert "李四中宝贝" in text
    assert "第0条" in text
    assert name.startswith("chat-0001-")


def test_uid_never_reaches_the_text():
    """🔑 uid 是**对话方的账号标识**（PII），不能进可检索正文。

    `name` 缺失时用它兜底**显示**可以，但不该写进正文 ——
    正文的每一段都会被切块、嵌入、进索引。
    """
    m = _msg(1)
    m["name"] = ""
    _, text = render_chat_slice([m], 1)
    assert "u_EXAMPLEopenid0000000000" not in text


def test_empty_text_message_keeps_a_line():
    """撤回/系统消息可能没有 text —— 整条丢掉会让记录看起来不完整。"""
    m = _msg(2)
    m["text"] = ""
    m["type"] = "recall"
    _, text = render_chat_slice([m], 1)
    assert "recall" in text


def test_crlf_is_normalised():
    """⚠️ 聊天导出常是 CRLF。不处理的话 chunk 里会塞满 `\\r`，
    归一化文本与词表都会变形（而且**不报错**）。"""
    m = _msg(3)
    m["text"] = "第一行\r\n第二行"
    _, text = render_chat_slice([m], 1)
    assert "\r" not in text
    assert "第一行" in text and "第二行" in text


def test_missing_ts_falls_back_to_date():
    m = _msg(4)
    m["ts"] = 0
    _, text = render_chat_slice([m], 1)
    assert "2024-07-21" in text


def test_slice_name_encodes_date_range():
    ms = [_msg(0), _msg(1)]
    ms[0]["date"], ms[1]["date"] = "2024-07-21", "2024-07-23"
    name, _ = render_chat_slice(ms, 7)
    assert name == "chat-0007-20240721-20240723.md"


# ============================================================
# 4. 读文件：畸形行不许毁掉整批
# ============================================================


def test_iter_messages_skips_malformed_lines(tmp_path):
    f = tmp_path / "chat.jsonl"
    f.write_text(
        json.dumps(_msg(0), ensure_ascii=False) + "\n"
        + "{不是 json}\n"
        + "\n"
        + "[1,2,3]\n"                      # 不是 dict
        + json.dumps(_msg(1), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    got = list(iter_messages(f))
    assert len(got) == 4, "坏行也要 yield（调用方要计数）"
    assert sum(1 for m in got if is_malformed(m)) == 2
    assert sum(1 for m in got if not is_malformed(m)) == 2


def test_count_messages_ignores_blank_lines(tmp_path):
    f = tmp_path / "chat.jsonl"
    f.write_text('{"a":1}\n\n\n{"a":2}\n\n', encoding="utf-8")
    assert count_messages(f) == 2


def test_count_messages_no_trailing_newline(tmp_path):
    """⚠️ 最后一行没有换行符也要算进去 —— 少算一个就是少导一个文档。"""
    f = tmp_path / "chat.jsonl"
    f.write_text('{"a":1}\n{"a":2}', encoding="utf-8")
    assert count_messages(f) == 2


def test_count_matches_iter(tmp_path):
    """🔑 估算值与实际读到的一致 —— 不一致的话 `estimated_docs` 就是骗人的。"""
    rows = [json.dumps(_msg(i), ensure_ascii=False) for i in range(101)]
    f = tmp_path / "chat.jsonl"
    f.write_text("\n".join(rows) + "\n\n", encoding="utf-8")
    assert count_messages(f) == len([m for m in iter_messages(f) if not is_malformed(m)])


# ============================================================
# 5. 目录扫描
# ============================================================


def _mk(d: Path, *names: str) -> None:
    for n in names:
        (d / n).write_bytes(b"x")


def test_scan_dir_sorted_and_filtered(tmp_path):
    _mk(tmp_path, "b.jpg", "a.png", "c.txt", "d.webp")
    got = [p.name for p in scan_dir(tmp_path, recursive=False, suffixes=IMAGE_SUFFIXES)]
    assert got == ["a.png", "b.jpg", "d.webp"], f"顺序与过滤：{got}"


def test_scan_dir_recursive(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _mk(tmp_path, "a.jpg")
    _mk(sub, "b.jpg")
    got = [p.name for p in scan_dir(tmp_path, recursive=True, suffixes=IMAGE_SUFFIXES)]
    assert got == ["a.jpg", "b.jpg"]
    flat = scan_dir(tmp_path, recursive=False, suffixes=IMAGE_SUFFIXES)
    assert [p.name for p in flat] == ["a.jpg"]


def test_scan_dir_is_deterministic(tmp_path):
    """🔑 两次扫描顺序必须一致 —— 否则"重跑一次"的 diff 大到没法看。"""
    for n in ("z.jpg", "m.jpg", "a.jpg", "q.jpg"):
        _mk(tmp_path, n)
    first = scan_dir(tmp_path, recursive=False, suffixes=IMAGE_SUFFIXES)
    second = scan_dir(tmp_path, recursive=False, suffixes=IMAGE_SUFFIXES)
    assert [p.name for p in first] == [p.name for p in second]


def test_scan_dir_respects_limit(tmp_path):
    """⚠️ 上限是"不会把 worker 拖死"的保险，不是装饰。"""
    for i in range(12):
        _mk(tmp_path, f"{i:03d}.jpg")
    got = scan_dir(tmp_path, recursive=False, suffixes=IMAGE_SUFFIXES, limit=10)
    assert len(got) == 10


def test_scan_dir_empty(tmp_path):
    assert scan_dir(tmp_path, recursive=True, suffixes=IMAGE_SUFFIXES) == []


# ============================================================
# 6. handler 注册：任务能入队 ≠ 能跑
# ============================================================


#: **已知缺口**（kind 没有 handler → 任务能入队、永远跑不了）。
#: ⚠️ 刻意写成显式集合而不是空集：补上一个就要从这里删掉，
#: 这样"缺口"这个事实不会因为没人盯着而消失。
#:
#: 历史：`import_chat`（D47）与 `pdf_preview`（D34 v1.1）曾在这个集合里，
#: 见 PROGRESS §5.49 与 §5.57。**现在所有 kind 都有 handler。**
KNOWN_KIND_GAPS: set[str] = set()


def test_every_kind_has_a_handler():
    """🔑 曾经 `import_chat` / `pdf_preview` 两个 kind **没有 handler**。

    worker 遇到未注册的 kind 只标失败不崩，于是任务能入队、永远跑不了
    （表现：202 成功、任务永远 pending，用户完全不知道出了什么事）。
    这个测试就是那道防线。
    """
    from services.worker.handlers import HANDLERS

    missing = set(ALL_KINDS) - set(HANDLERS)
    assert missing == KNOWN_KIND_GAPS, (
        f"kind 缺口变了：多出来 {sorted(missing - KNOWN_KIND_GAPS)}"
        f"（要么补 handler，要么更新 KNOWN_KIND_GAPS 并记进 PROGRESS）"
    )


def test_import_kinds_are_not_gaps_anymore():
    """`import_chat` / `import_dir` 必须有 handler（D47/D48）。"""
    from services.worker.handlers import HANDLERS

    assert "import_chat" in HANDLERS
    assert "import_dir" in HANDLERS


def test_import_handlers_declare_report():
    """导入类 handler **必须**声明 `report`，否则 payload 计数器永远不写。

    （`progress` 只能回一个浮点，写不了 `processed_msgs`。）
    """
    import inspect

    from services.worker.handlers import handle_import_chat, handle_import_dir

    for fn in (handle_import_chat, handle_import_dir):
        assert "report" in inspect.signature(fn).parameters, (
            f"{fn.__name__} 没有 report 参数 → 计数器写不进 /jobs"
        )


def test_plain_handlers_do_not_declare_report():
    """反过来也要钉住：不需要计数器的 handler 不该多带一个参数。"""
    import inspect

    from services.worker.handlers import handle_reindex, handle_thumbnail

    for fn in (handle_reindex, handle_thumbnail):
        assert "report" not in inspect.signature(fn).parameters


def test_run_handler_passes_only_what_is_declared():
    """`run_handler` 靠签名裁参数 —— 给所有 handler 一律传 5 个 kwargs
    会逼每个 handler 都写一个用不到的 `report=None`。"""
    import inspect

    from services.worker.handlers import run_handler

    params = inspect.signature(run_handler).parameters
    assert {"job_id", "report"} <= set(params), params


# ============================================================
# 7. 归属判定在 services 侧可用（§0.1 的搬家结果）
# ============================================================


def test_classifier_is_importable_from_services():
    """导入任务要重跑判定（D47），而它在 `services/` 里 —— 不依赖 `pipeline/`。"""
    from services.ingest.privacy import classify_owner

    assert classify_owner("正文", "赵六-自传.md")["owner"] == "other"