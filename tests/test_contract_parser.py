"""🔌 `eval/contract_check.py` 的**解析器**守卫（不是端点存在性）。

## 为什么需要它

`contract_check.py` 是"契约覆盖率"这个数字的唯一来源，而它的解析器
（`_DECL_RE` + `_decl_path`）**没有任何测试**。D151 我为了加"query 参数比对"
把正则改成"不在 `?` 处截断"，忘了 `declared_endpoints()` 用**同一个正则**的
`group(2)` 当路径 ⇒ 路径变成 `/api/v1/kb?workspace_id=1&…` ⇒ **全部**端点
都匹配不上实现：

```
覆盖率 71/72  →  58/75 · 退出码 1
```

症状极具欺骗性：它看起来像"契约与实现脱节了"，而真实原因是**解析器被我改坏**。
一个"报告数字的工具"坏掉时，**它报的数字本身也是错的** —— 而那正是别人唯一会看的东西。

## 三条判据

1. **`_decl_path()` 必须剥掉 query string 与路径变量**（`/api/v1/kb/{kb_id}?x=1`
   → `/api/v1/kb/{}`）
2. **端点数与覆盖率不能因为解析器改动而变**（钉住当前真实值：75 / 74）
3. **参数面与端点面必须共用同一个路径解析**（否则两边看到的"端点"不是同一个，
   假参数会永远比不出来）

⚠️ **盲区**（写下来是为了下一个人知道边界）：这里只测**解析**，不测"契约里写了
一个实现没有的端点"会被报出来 —— 那由 `--strict` 的实跑覆盖（见 `MEMORY.md`
基线里的 `contract_check`）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "eval") not in sys.path:
    sys.path.insert(0, str(ROOT / "eval"))

import contract_check as cc  # noqa: E402


# ============================================================
# 1. 路径解析
# ============================================================


def test_decl_path_strips_query_and_path_params():
    """🔑 `GET /api/v1/kb/{kb_id}?x=1` → `/api/v1/kb/{}`（D151 的回归点）。

    ⚠️ 判据是**三样都剥掉**：query string、路径变量名、以及不留前导斜杠差异。
    只测"剥不剥 query"会漏掉"路径变量没归一化"（那样端点在契约与实现之间
    也对不上，而症状同样是"覆盖率掉了"）。
    """
    decl = cc._DECL_RE.search("GET /api/v1/kb/{kb_id}?workspace_id=1&page=2\n")
    assert decl is not None, "_DECL_RE 匹配不到 'METHOD /path?query' —— 契约格式变了？"
    assert cc._decl_path(decl) == "/api/v1/kb/{}"

    plain = cc._DECL_RE.search("POST /api/v1/admin/reindex\n")
    assert plain is not None
    assert cc._decl_path(plain) == "/api/v1/admin/reindex"


def test_decl_path_handles_a_doc_line_with_a_trailing_arrow():
    """🔑 契约里很多行后面跟着 `→ 说明`，路径不能把说明吃进去。"""
    decl = cc._DECL_RE.search(
        "GET    /api/v1/kb/trash → 回收站列表（裸数组 KbOut）\n"
    )
    assert decl is not None
    assert cc._decl_path(decl) == "/api/v1/kb/trash"


# ============================================================
# 2. 端点数（钉住真实值 —— 解析器一坏这里就红）
# ============================================================


def test_endpoint_count_is_stable():
    """🔑 契约声明 **82** 个端点（D163 +3；D170 +3；D173 用户管理 +3）。

    ⚠️ 这条是**故意**把当前数字钉死的：解析器一坏，声明数会变
    （实测覆盖率从 71/72 掉到 58/75，而声明数因为 `?` 后被当成路径而**变大**）。
    无论哪种，数字一变就说明解析器被改过 ⇒ 先看解析器，别急着改契约。
    """
    assert len(cc.declared_endpoints()) == 82, (
        f"契约端点数变成 {len(cc.declared_endpoints())} 了 —— "
        "先查 `_decl_path` / `_DECL_RE` 是不是被改坏（D151 的回归形状）"
    )


def test_every_declared_endpoint_matches_an_implemented_one():
    """🔑 除已决议"不做"的那一个，其余都实现（`--strict` 的前置条件）。"""
    declared = cc.declared_endpoints()
    refused = cc.refused_endpoints()
    impl = cc.implemented_endpoints()
    missing = sorted(k for k in declared if k not in refused and k not in impl)
    assert not missing, f"契约声明了但没实现：{missing}"
    assert len(impl) == 81, f"实现的 operation 数变成 {len(impl)} 了"
    # 反向：被决议"不做"却实现了 —— 那是偏离决议
    assert not [k for k in refused if k in impl], "有端点与「明确不做」的决议相反地实现了"


# ============================================================
# 3. 参数面与端点面必须看到同一个路径
# ============================================================


def test_declared_params_share_the_same_path_parser():
    """🔑 两个面共用 `_decl_path` —— 否则"同一个端点"两边不一致，假参数永远比不出来。

    ⚠️ 第一版 `declared_query_params()` 自己 `partition("?")` 了一遍，
    `declared_endpoints()` 直接拿 `group(2)` —— 两套解析。
    D151 的回归就是这么发生的（改了正则，两处一起坏）。
    """
    src = (ROOT / "eval" / "contract_check.py").read_text(encoding="utf-8")
    tree = __import__("ast").parse(src)
    funcs = {n.name: n for n in __import__("ast").walk(tree)
             if isinstance(n, __import__("ast").FunctionDef)}
    for name in ("declared_endpoints", "declared_query_params",
                 "refused_endpoints"):
        body = __import__("ast").unparse(funcs[name])
        assert "_PARAM_RE.sub" not in body, (
            f"{name}() 还在自己归一化路径 —— 应该走 _decl_path()，"
            "否则三处解析会各自漂"
        )
        assert "_decl_path(m)" in body, f"{name}() 没走 _decl_path()"


def test_decl_regex_keeps_the_query_string():
    """🔑 `_DECL_RE` **必须**保留 `?…`（参数面全靠它）。

    ⚠️ 第一版没有这条，变异脚本把正则退回 `/[^\s?]*`（在 `?` 处截断）时**全绿** ——
    因为 `declared_query_params()` 靠 `partition("?")` 拿参数，而正则已经截掉的话
    它拿到空串，于是**整张参数表变成空的**，可那条测试只查"抽出来的名字形状对不对"
    —— 空表当然形状都对。
    ⇒ 这里直接断言"正则吃进去的那一段里还有 `?`"，也就是最直接的行为。
    """
    m = cc._DECL_RE.search("GET /api/v1/kb?workspace_id=1\n")
    assert m is not None
    assert "?" in m.group(2), (
        "_DECL_RE 在 `?` 处截断了 —— query 参数面会变成空表，"
        "而空表'形状都对'，别的断言抓不到（D151 变异脚本第 3 条）"
    )


def test_implemented_params_come_from_openapi_not_app_routes():
    """🔑 实现侧必须覆盖**全部**路由（不能用 `app.routes`）。

    🔴 D151 实测：这版 FastAPI 把 `include_router` 的结果包成 `_IncludedRouter`
    对象（**不扁平**），所以 `app.routes` 里 `isinstance(r, APIRoute)` **只剩 2 条**
    （catch-all + `/`）⇒ 参数面会变成"几乎所有端点都没有参数"，
    而**假参数也就永远比不出来**（没有参数可对照）。

    ⚠️ 第一版守卫只查契约侧（`declared_query_params`），于是这个变异全绿 ——
    契约侧压根不碰 `app.openapi()`。
    """
    impl = cc.implemented_query_params()
    # 71 个 operation 里绝大多数带 query 参数；用 app.routes 时这里会只剩 1~2 个
    assert len(impl) >= 30, (
        f"实现侧只认出 {len(impl)} 个端点的参数 —— "
        "是不是用了 `app.routes`？（D151：那版 FastAPI 不扁平，只有 2 条）"
    )
    for key in (("GET", "/api/v1/kb"), ("GET", "/api/v1/jobs"),
                ("GET", "/api/v1/kb/{}")):
        assert key in impl, f"实现侧少了 {key} —— 端点没被展开？"
    assert "workspace_id" in impl[("GET", "/api/v1/kb")], impl[("GET", "/api/v1/kb")]
    # 而 catch-all（SPA 回退）不该出现在参数表里 —— 它不是 API 端点
    assert ("GET", "/{}") not in impl, "SPA catch-all 被当成 API 端点了"


def test_declared_query_params_are_not_empty():
    """🔑 真契约里**确实**抽得到参数（防"整张表悄悄变空"）。

    ⚠️ 这是上一条的兜底：任何让参数表变空的原因（正则截断、模板前缀清空、
    `GLOBAL_PARAMS` 写错）都会在这里露出来，而不是等到"某天没人查契约参数"。
    """
    d = cc.declared_query_params()
    assert len(d) >= 10, f"契约里带 query 参数的端点只剩 {len(d)} 个 —— 参数面是不是空了？"
    assert any("kb_id" in names or "workspace_id" in names
               for names in d.values()), d


def test_declared_query_params_are_real_params():
    """🔑 抽出来的参数名不含 `?` / `=` / 空串（解析正确的形状检查）。"""
    for (method, path), names in cc.declared_query_params().items():
        assert path.startswith("/") and "?" not in path, f"{method} {path}"
        for n_ in names:
            assert n_ and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n_), (
                f"{method} {path} 的参数名 {n_!r} 不像参数名"
            )
    # 全局分页参数不该逐端点重复声明（§0.4 统一说过）
    allnames = {n for names in cc.declared_query_params().values() for n in names}
    assert "page" not in allnames and "page_size" not in allnames, (
        "page/page_size 被逐端点声明了 —— §0.4 说过它们是全局的"
    )


def test_fake_params_are_detected(tmp_path, monkeypatch):
    """🔑 往契约里塞一个**代码没有**的参数 ⇒ 必须被判成假参数。

    ⚠️ 用真契约文件做不了（会写脏仓库），所以这里临时换 `cc.CONTRACT` 指向
    tmp 下的副本，并同时换 `implemented_query_params` 的返回 ——
    验证的是**判定逻辑**，不是真实契约的内容。
    """
    fake = tmp_path / "CONTRACT.md"
    fake.write_text(
        "```http\nGET /api/v1/kb?workspace_id=1&ghost_param=2\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cc, "contract_files", lambda: [fake])
    monkeypatch.setattr(
        cc, "implemented_query_params",
        lambda: {("GET", "/api/v1/kb"): {"workspace_id"}},
    )
    d = cc.declared_query_params()
    assert d[("GET", "/api/v1/kb")] == {"workspace_id", "ghost_param"}, d
    real = cc.implemented_query_params()[("GET", "/api/v1/kb")]
    assert sorted(d[("GET", "/api/v1/kb")] - real) == ["ghost_param"]


def test_main_reports_and_fails_on_a_fake_param(tmp_path, monkeypatch, capsys):
    """🔑 假参数必须**被报出来且退出码非 0**（不是只算出来）。

    ⚠️ 第一版只验证了"假参数能被算出来"（集合减法），而变异脚本把
    `bad = extra + refused_but_built + [k for k, _ in fake_params]`
    里的第三项删掉时**照样全绿** —— 因为那条判定发生在 `main()` 里，
    而那条测试根本没调 `main()`。
    ⇒ 这里真的跑一次 `main()`，断言**输出里有那一行**且**退出码非 0**。
    """
    fake = tmp_path / "CONTRACT.md"
    fake.write_text(
        "```http\nGET /api/v1/kb?workspace_id=1&ghost_param=2\n"
        "POST /api/v1/kb\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cc, "contract_files", lambda: [fake])
    monkeypatch.setattr(
        cc, "implemented_query_params",
        lambda: {("GET", "/api/v1/kb"): {"workspace_id"}},
    )
    monkeypatch.setattr(
        cc, "implemented_endpoints",
        lambda: {("GET", "/api/v1/kb"), ("POST", "/api/v1/kb")},
    )
    # ⚠️ `main()` 自己 `parse_args()` ⇒ 在 pytest 里会吃到 pytest 的 argv
    #   （`-q -k …`）⇒ SystemExit: 2。第一版没注意，报错完全指不到
    #   "是那个脚本在读命令行"。
    monkeypatch.setattr(sys, "argv", ["contract_check.py"])
    rc = cc.main()
    out = capsys.readouterr().out
    assert "ghost_param" in out, f"没报出假参数：\n{out}"
    assert "假参数" in out or "query 参数" in out, out
    assert rc != 0, "报了假参数却仍然退出 0 —— 接线断了"
