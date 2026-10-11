"""🔴 D140：格式**派发表是唯一一份清单**（M4 承诺"新格式注册即可，不改调度逻辑"）。

## 以前的三份清单

| 位置 | 内容 | 加一种格式要改吗 |
|---|---|---|
| `SUPPORTED_SUFFIXES`（模块顶部）| 9 个文档后缀 | 要 |
| `IMAGE_SUFFIXES`（模块顶部）| 6 个图片后缀 | 要 |
| `parse_to_chunks` 的 if 链 | 同一批后缀，再写一遍 | 要 |

⇒ **漏一处的后果分两种，而且都不直观**：

- 只改白名单、忘了分派 ⇒ 用户**上传成功**（201），worker 在**摄取时**报
  `UnsupportedFormat` —— 错误出现在 job 上，不在上传响应上，
  用户的直觉是"文件坏了"而不是"服务端不支持这个格式"
- 只改分派、忘了白名单 ⇒ 上传直接 **415**，而格式明明支持

## 现在的形状

`PARSERS`（文件末尾）是**唯一**清单；`SUPPORTED_SUFFIXES` / `IMAGE_SUFFIXES`
由 `_derive_suffix_sets(PARSERS)` **派生**，分派也查同一张表。
⇒ **加一种格式 = 加一条记录**。

⚠️ 判据要能抓住"又手写了一份"：所以断言的是
`SUPPORTED_SUFFIXES == _derive_suffix_sets(PARSERS)[0]`，
而不是"它等于那 9 个后缀"（后者对手写与派生**同样成立**，抓不住）。
"""

from __future__ import annotations
from _docs import contract_text

import ast
from pathlib import Path

import pytest

from services.ingest import parsers
from services.ingest.parsers import (
    IMAGE_SUFFIXES,
    PARSERS,
    SUPPORTED_SUFFIXES,
    _derive_suffix_sets,
    parse_to_chunks,
)

ROOT = Path(__file__).resolve().parents[1]
PARSERS_PY = ROOT / "services" / "ingest" / "parsers.py"


def test_whitelists_are_derived_not_handwritten():
    """🔑 两个白名单常量 == 派发表推导出来的结果。"""
    assert _derive_suffix_sets(PARSERS) == (SUPPORTED_SUFFIXES, IMAGE_SUFFIXES), (
        "白名单与派发表不一致 —— 说明有人又手写了一份清单（D140 的整件事就是这个）"
    )


def test_derive_splits_on_the_scanned_marker():
    """🔑 文档/图片之分来自 `scanned` 标记，不是另一份清单。"""
    docs, images = _derive_suffix_sets(PARSERS)
    assert ".pdf" in docs and ".docx" in docs
    assert ".jpg" in images and ".png" in images
    assert not (docs & images), "同一个后缀不能既是文档又是图片"
    assert docs | images == set(PARSERS)


def test_adding_one_entry_changes_the_whitelist():
    """🔑 **派生**的性质：新加一条 ⇒ 上传白名单自动跟着变。

    ⚠️ 这条是"派生"与"手写"的分水岭：手写白名单时，
    只改派发表是**看不出**上传端点会不会放行的。
    """
    sentinel = object()
    trial = dict(PARSERS)
    trial[".zzz"] = parsers._Parser(lambda p, s, o: ([], [], False))
    docs, images = _derive_suffix_sets(trial)
    assert ".zzz" in docs and ".zzz" not in images
    assert ".zzz" not in SUPPORTED_SUFFIXES, "本测试不该影响真实常量"


@pytest.mark.parametrize("suffix", sorted(PARSERS))
def test_every_registered_suffix_is_dispatchable(suffix, tmp_path):
    """🔑 表里每一条都真的能被分派到（用探针替换解析函数，不依赖真实文件）。

    ⚠️ 判据是"**那个 `fn` 被调用了**"，不是"没抛异常"——
    `.pdf`/`.docx` 这些要真实文件才能跑通，用"没抛异常"当判据的话
    表里放一个错的名字也会绿。
    """
    calls: list[tuple] = []
    entry = PARSERS[suffix]
    original = entry.fn
    entry.fn = lambda p, s, o: (calls.append((p, s, o)) or ([], [], False))
    try:
        parse_to_chunks(tmp_path / "blob", file_name=f"x{suffix}",
                        chunk_size=7, chunk_overlap=3)
    finally:
        entry.fn = original
    assert calls == [(tmp_path / "blob", 7, 3)], f"{suffix} 没被分派到：{calls}"
    # ⚠️ 判据取**注册表上的标记**，不是探针的返回值（探针一律返回
    #   is_scanned=False，拿它判图片后缀必然失败——第一版就这么写的）。
    #   真实图片分支的行为由下面那条单独钉。
    assert entry.scanned == (suffix in IMAGE_SUFFIXES), (
        f"{suffix} 的 scanned 标记与派发表不符"
    )


def test_unknown_suffix_still_raises_with_the_suffix_in_the_message():
    """🔑 不支持的格式仍然报 `UnsupportedFormat`，**且消息里有那个后缀**。

    ⚠️ 消息里有后缀是运维排障的关键（用户截图里只有"不支持的格式"，
    而支持哪些要看另一个文档）。判据是消息**含**它，不是整句相等。
    """
    with pytest.raises(parsers.UnsupportedFormat) as ei:
        parse_to_chunks(Path("/tmp/blob"), file_name="x.exe")
    assert ".exe" in str(ei.value), str(ei.value)
    # 顺带：没有扩展名也要给出可行动的提示
    with pytest.raises(parsers.UnsupportedFormat) as ei2:
        parse_to_chunks(Path("/tmp/blob"), file_name="noext")
    assert "无扩展名" in str(ei2.value), str(ei2.value)


def test_dispatch_has_no_if_chain_left():
    """🔑 `parse_to_chunks` 里**不许**再有按后缀的 `if`（D140 的形状本身）。

    ⚠️ 判据是"没有比较字符串字面量的 `if`"，不是"没有 `if`"——
    将来真需要按后缀做别的判断（比如 `.csv` 走分隔符推断）也不该被这条挡住。
    """
    tree = ast.parse(PARSERS_PY.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "parse_to_chunks")
    suffix_ifs = [
        ast.unparse(n.test) for n in ast.walk(fn)
        if isinstance(n, ast.If) and "suffix" in ast.unparse(n.test)
    ]
    assert suffix_ifs == [], f"parse_to_chunks 里还有按后缀分支：{suffix_ifs}"


def test_upload_endpoint_accepts_exactly_the_registry():
    """🔑 **上传端点**放行的集合 == 派发表的键（两份清单漂移就会红）。

    🔑 这是 D140 真正要防的那条：上传白名单是**导入**进来的常量，
    所以这里断言的是"端点用的那个集合 == 派表的键"，而不是"它等于某几个后缀"。
    """
    from services.api.routers import documents as doc_router

    assert doc_router.SUPPORTED_SUFFIXES is parsers.SUPPORTED_SUFFIXES
    assert doc_router.IMAGE_SUFFIXES is parsers.IMAGE_SUFFIXES
    assert set(doc_router.SUPPORTED_SUFFIXES) | set(doc_router.IMAGE_SUFFIXES) \
        == set(PARSERS)


def test_image_branch_still_returns_scan_page_one():
    """🔑 图片仍返回 `scan_pages=[1]` + `is_scanned=True`。

    ⚠️ 这两个值是"走视觉腿"的唯一判据（`CONVENTIONS` §6）：
    少了 `scan_pages` 就不会入队视觉任务，少了 `is_scanned` 就走文本分支。
    """
    out = parse_to_chunks(Path("/tmp/whatever"), file_name="a.JPG")
    assert out == ([], [1], True), out
    assert out[1] == [1] and out[2] is True

# ============================================================
# 两条"运行时分辨不出来"的性质 ⇒ 要靠**别的来源**交叉验证
# ============================================================


def test_whitelist_assignment_is_a_derivation_call():
    """🔑 那两个常量**必须**由 `_derive_suffix_sets(PARSERS)` 赋值。

    ⚠️ 这条判据的作用要说清楚：`test_whitelists_are_derived_not_handwritten`
    在"手写但值恰好相同"时是**绿的** —— 运行时无法区分"派生"与"手写且相等"
    （第一版把它当成能抓漂移的判据，被变异脚本直接证伪：§5.136）。
    ⇒ 唯一能看到"是不是手写"的地方是**源码里的赋值形状**：
    右值必须是 `_derive_suffix_sets(PARSERS)` 这个调用。
    """
    tree = ast.parse(PARSERS_PY.read_text(encoding="utf-8"))
    state: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not (isinstance(tgt, ast.Tuple)
                and [e.id for e in tgt.elts] == ["SUPPORTED_SUFFIXES", "IMAGE_SUFFIXES"]):
            continue
        value = node.value
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "_derive_suffix_sets"):
            state["ok"] = ast.unparse(value)
        else:
            state["手写"] = ast.unparse(value)[:80]
    assert "手写" not in state, (
        f"SUPPORTED_SUFFIXES/IMAGE_SUFFIXES 被手写了（{state.get('手写')}）—— "
        "那样它们就是第二份清单，与 PARSERS 会漂"
    )
    assert state.get("ok") == "_derive_suffix_sets(PARSERS)", state


#: 允许"手写后缀集合"的**唯一**一处，附理由（而不是整类放行）。
#:
#: `ingest/office.py::OFFICE_SUFFIXES` 回答的是"LibreOffice 能转哪些"，
#: 刻意是上传白名单的**超集**。豁免必须有理由 + 一条关系测试（见下面那条）。
SUFFIX_SET_EXEMPT: dict[str, str] = {
    "ingest/office.py": "LibreOffice 的能力表，刻意是 PARSERS 的超集（D140）",
}


def test_the_exemption_is_still_needed():
    """🔑 豁免不能变成"忘了删"：一旦 `OFFICE_SUFFIXES` 不再是超集，
    就该把豁免去掉（否则它会放过真正的手写清单）。"""
    from services.ingest.office import OFFICE_SUFFIXES

    office_in_registry = {".docx", ".xlsx", ".pptx"} & set(PARSERS)
    assert office_in_registry <= OFFICE_SUFFIXES, (
        "PARSERS 里的 Office 格式不在 OFFICE_SUFFIXES 里 ⇒ "
        "office.py 那份已经不只是超集，该重新想它的定位"
    )
    assert len(OFFICE_SUFFIXES) > len(office_in_registry), (
        "OFFICE_SUFFIXES 已经等于 PARSERS 的 Office 部分 ⇒ "
        "它不再需要独立存在，把 test_parser_registry 的豁免删掉"
    )


def test_no_other_module_hardcodes_a_suffix_set():
    """🔑 `services/` 下**别处**不许再出现"一堆 `.后缀`"的字面集合。

    ⚠️ 判据是"字面量里所有元素都是以 `.` 开头的字符串"，不是匹配某几个已知后缀
    —— 后者只能抓"抄了同一份"，抓不住"抄了一份**新的**"。
    🔑 这条判据**当场抓到了一个真缺陷**：`api/routers/documents.py::_is_image`
    又手写了一遍 6 个图片后缀，而它被三处用（预览/下载/媒体类型）——
    加一种图片格式时那三处会悄悄不认。
    """
    import services

    pkg = Path(services.__file__).parent
    offenders: list[str] = []
    for f in pkg.rglob("*.py"):
        rel = str(f.relative_to(pkg))
        if f.name == "parsers.py":
            continue
        if rel in SUFFIX_SET_EXEMPT:
            assert SUFFIX_SET_EXEMPT[rel], f"{rel} 的豁免没写理由"
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
                continue
            strs = [e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(strs) >= 3 and all(s.startswith(".") and len(s) > 2 for s in strs):
                offenders.append(f"{f.relative_to(pkg)}:{node.lineno} {strs}")
    assert not offenders, (
        "这些地方**手写**了扩展名集合（格式清单的唯一来源是 parsers.PARSERS）："
        + "; ".join(offenders)
    )


def test_registry_matches_the_formats_written_in_the_contract():
    """🔑 `PARSERS` 的键 == **契约里写的那份**（`docs/DESIGN-API.md`）。

    🔑 这是唯一能抓住"派发表里有个**错字**后缀"的判据 ——
    `.json` 写成 `.json2` 时，派生与派发**仍然自洽**（实测过），
    只有跟**另一个来源**（契约）比才能发现。
    ⚠️ 顺带保证契约不会漂：契约里那份是从派发表抄的，改代码不改契约就会红。
    """
    import re

    contract = contract_text()
    row = next((ln for ln in contract.splitlines()
                if ln.startswith("| 白名单内容 |")), None)
    assert row is not None, (
        "契约里没有「白名单内容」那一行 —— 补上，否则格式清单只剩代码里一份，"
        "没人能对照"
    )
    in_contract = frozenset(re.findall(r"`(\.[a-z0-9]+)`", row))
    assert in_contract == set(PARSERS), (
        f"契约里的格式清单与 PARSERS 不一致：\n"
        f"  只在契约里：{sorted(in_contract - set(PARSERS))}\n"
        f"  只在代码里：{sorted(set(PARSERS) - in_contract)}"
    )


def test_every_registered_parser_returns_the_triple_shape():
    """🔑 派发表里**每一条**都保证返回 `(chunks, scan_pages, is_scanned)`。

    🔴 这条是 D140 第一版踩的坑：`_parse_text` / `_parse_docx` / `_parse_xlsx` /
    `_parse_pptx` 都只返回 `list[Chunk]`，以前是**调用点**给它们补的
    `, [], False` —— 换成注册表之后那层不见了，于是
    `ValueError: not enough values to unpack (expected 3, got 1)`。
    ⇒ 派发表的契约是**一个形状**，归一在**注册时**做。

    ⚠️ 判据用 `.md`/`.txt` **真跑**（这两个造文件不要钱），其余格式用
    「都声明了 `unified`」这条结构性质 —— 因为给 `.pdf`/`.docx` 造真实文件
    太贵，而这个守卫要防的是"注册了一个返回形状不对的解析器"。
    """
    assert all(getattr(p.fn, "unified", None) for p in PARSERS.values()), (
        "有派发记录没有声明返回形状（`unified`）—— "
        f"未声明的：{[e for e, p in PARSERS.items() if not getattr(p.fn, 'unified', None)]}"
    )

    # 真跑两个最便宜的格式
    for suffix in (".md", ".txt"):
        f = Path(__file__).parent / f"_probe{suffix}"
        try:
            f.write_text("# 标题\n\n" + "内容段落。" * 40, encoding="utf-8")
            chunks, pages, scanned = parse_to_chunks(f, file_name=f"x{suffix}")
        finally:
            f.unlink(missing_ok=True)
        assert isinstance(chunks, list) and chunks, f"{suffix} 没有切出 chunk"
        assert pages == [] and scanned is False, (suffix, pages, scanned)


def test_adapter_rejects_a_parser_that_returns_a_triple():
    """🔑 适配器里那个 `assert` 不是装饰：有人把某个解析器改成自己返回三元组，
    会被立刻炸出来，而不是**套成** `((a, b, c), [], False)` 这种更坏的东西。"""
    wrapped = parsers._as_document_parser(lambda p, s, o: ([], [], False))
    with pytest.raises(AssertionError):
        wrapped(Path("/tmp/x"), 1, 1)
