"""契约漂移检查：`docs/DESIGN-API.md` 声明的端点 ↔ 代码里真实存在的端点。

**为什么要有这个脚本**：契约漂移在本项目已经发生两次，两次都是"人工发现"：

1. 页图路径 —— 契约写 `/pages/{n}/image`，实现是 `/pages/{n}`
2. `/preview` 的返回类型 —— 契约写 `application/pdf`，实现是 `image/jpeg`

两次的共同点是：**当时都不影响已写好的代码**（1e 的前端是照实现路径写的，
不是照契约），所以没有任何测试会红。等到阶段 2 照契约新写前端时才咬人 ——
那时候的成本是"前端跑不起来 + 排查半天才发现是契约写错了"。

⚠️ 这个脚本只查**路径与方法**（自动可判）。字段名、返回类型这类形状漂移
查不出来 —— 那些已在 DESIGN-API.md 里就地标注"以代码为准"。

跑法（不需要起服务）：
    uv run python eval/contract_check.py
    uv run python eval/contract_check.py --strict    # 有未实现端点就退出码 1
"""

from __future__ import annotations

import os

# 🔴 离线加载模型（说明见 tests/conftest.py）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 契约在 `DESIGN-API*.md`（当前一份；glob 兼容历史上拆成多份的形态）—— **全部**要读。
CONTRACT_DIR = ROOT / "docs"


def contract_files() -> list[Path]:
    """`docs/DESIGN-API*.md`（当前一份），按名字排序。"""
    return sorted(CONTRACT_DIR.glob("DESIGN-API*.md"))


def contract_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in contract_files())

#: ```http 代码块里的 `METHOD /path`，形如 `GET /api/v1/kb/{id}`
#:
#: ⚠️ **不在 `?` 处截断**（D151）：契约里那些
#:   `GET /api/v1/kb?workspace_id=1&visibility=private&page=1` 的**参数**
#:   从来没被校验过 —— 而 FastAPI 对**未知 query 参数静默忽略**，
#:   与契约自己警告的 `PATCH` body 那个坑同形（"传了没生效"的第一问是
#:   "它在不在清单里"，只是没人给 query 参数做清单）。
#:   ⇒ `declared_endpoints()` 归一化路径时把 `?...` 摘掉，
#:   `declared_query_params()` 单独取它。
_DECL_RE = re.compile(r"^\s*(GET|POST|PATCH|DELETE|PUT)\s+(/[^\s]*)", re.M)
#: 路径参数归一化：`{kb_id}` / `{id}` / `{n}` 一律变成 `{}`
_PARAM_RE = re.compile(r"\{[^}]+\}")
#: §0.4 的分页示例是 `GET /xxx?...`，那是**模板**不是真端点。
#: 别把阶段 5 的 `/mcp` `/a2a` 也加进来 —— 它们是真端点，只是还没实现。
_TEMPLATE_PREFIXES = ("/xxx",)


#: 契约里标记「❌ 明确不做」的行（决议过，不是待办）。见 D96。
_REFUSED_MARK = "明确不做"


def _decl_path(m: re.Match[str]) -> str:
    """从一条声明里取出**归一化路径**（剥掉 `?…` 与路径变量名）。

    🔴 D151 第一版的回归：为了取 query 参数，我把 `_DECL_RE` 改成"不在 `?` 处截断"，
    却忘了 `declared_endpoints()` 用**同一个正则的 group(2)** 当路径 ——
    于是路径变成 `/api/v1/kb?workspace_id=1&…`，**全部**端点都匹配不上实现：
    覆盖率从 71/72 掉到 58/75、退出码 1。
    症状有欺骗性：它看起来像"契约与实现脱节了"，而真实原因是**解析器被我改坏了**。
    ⇒ 两个正则（端点 / 参数）各自取自己那部分，别共用一个"取路径"的表达式。
    """
    raw_path = m.group(2).partition("?")[0]
    return _PARAM_RE.sub("{}", raw_path)


def declared_endpoints() -> dict[tuple[str, str], int]:
    """从契约里抽出 (METHOD, path) → 出现次数。"""
    text = contract_text()
    out: dict[tuple[str, str], int] = {}
    for m in _DECL_RE.finditer(text):
        path = _decl_path(m)
        if any(path.startswith(p) for p in _TEMPLATE_PREFIXES):
            continue
        key = (m.group(1), path)
        out[key] = out.get(key, 0) + 1
    return out


#: §0.4 统一声明过 `page` / `page_size`（每个列表端点都收）——
#:   不逐个端点重复声明，所以比对时要排除，否则全是假缺口。
GLOBAL_PARAMS = frozenset({"page", "page_size"})


def declared_query_params() -> dict[tuple[str, str], set[str]]:
    """契约里每个端点写了哪些 **query 参数**（路径变量除外）。

    🔑 判据是"**契约写了但代码没有**"（⇒ 那条参数是假的、调用方传了没用），
    反向不判为错：代码多出来的参数（`doc_id` / `kind` 之类）只提示不失败 ——
    多一个能用的参数不会让按契约写的调用方出错，而少一个会。
    """
    out: dict[tuple[str, str], set[str]] = {}
    for m in _DECL_RE.finditer(contract_text()):
        path, _, qs = m.group(2).partition("?")
        norm = _decl_path(m)
        if any(norm.startswith(p) for p in _TEMPLATE_PREFIXES):
            continue
        names = {kv.split("=")[0].strip() for kv in qs.split("&") if kv.strip()}
        names -= GLOBAL_PARAMS
        if names:
            out.setdefault((m.group(1), norm), set()).update(names)
    return out


def implemented_query_params() -> dict[tuple[str, str], set[str]]:
    """真实路由的 query 参数。

    ⚠️ **必须用 `app.openapi()` 而不是 `app.routes`**（D151 实测）：
    这版 FastAPI 把 `include_router` 的结果包成 `_IncludedRouter` 对象
    （**不扁平**），于是 `isinstance(r, APIRoute)` 只剩 2 条（catch-all + `/`），
    看上去"所有端点都不存在"。
    """
    from services.api.main import app

    out: dict[tuple[str, str], set[str]] = {}
    for raw_path, ops in app.openapi()["paths"].items():
        for method, op in ops.items():
            if method not in ("get", "post", "patch", "delete", "put"):
                continue
            qs = {p["name"] for p in op.get("parameters") or []
                  if p.get("in") == "query"}
            key = (method.upper(), _PARAM_RE.sub("{}", raw_path))
            out[key] = out.get(key, set()) | qs
    return out


def refused_endpoints() -> dict[tuple[str, str], str]:
    """契约里标了「❌ 明确不做」的端点 → 决议摘要。

    ⚠️ 判定范围是**该行文本**（从 `METHOD /path` 起，到行尾）。
    只看端点所在段落会把下一行的正常端点也带进来。
    """
    out: dict[tuple[str, str], str] = {}
    for line in contract_text().splitlines():
        m = _DECL_RE.search(line)
        if not m or _REFUSED_MARK not in line:
            continue
        key = (m.group(1), _decl_path(m))
        # ⚠️ 只取标记后**第一行**的摘要，并去掉 markdown 强调符 ——
        #   契约里那行写的是 `❌ **明确不做**（D96，不是待办）`，
        #   原样截出来会是 `**（D96，不是待办` 这种半截话。
        tail = line.split(_REFUSED_MARK, 1)[1]
        tail = tail.split("不是待办", 1)[0] if "不是待办" in tail else tail
        note = tail.strip(" —（）()：:*")
        out[key] = (note[:60] if note else "已决议不做")
    return out


def implemented_endpoints() -> set[tuple[str, str]]:
    """从 `app.openapi()` 抽真实路由。"""
    from services.api.main import app

    paths = app.openapi()["paths"]
    return {
        (method.upper(), _PARAM_RE.sub("{}", path))
        for path, ops in paths.items()
        for method in ops
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--strict",
        action="store_true",
        help="有「声明了但没实现」的端点就退出码 1（接 CI 用）",
    )
    args = ap.parse_args()

    declared = declared_endpoints()
    refused = refused_endpoints()
    impl = implemented_endpoints()

    # 🔑 已决议不做的**不算缺口** —— 否则清单里会永远挂着一条
    #   "还没实现"的待办，而它的理由写在 REQUIREMENTS 里，
    #   三个月后有人照着实现就偏离了决议（D96）。
    todo = {k: v for k, v in declared.items() if k not in refused}
    missing = sorted(k for k in todo if k not in impl)
    extra = sorted(k for k in impl if k not in declared)
    #: 反向：被决议"不做"却实现了 —— 那是偏离决议，必须有人看见
    refused_but_built = sorted(k for k in refused if k in impl)

    total_declared = len(declared)
    total_impl = len(impl)

    print(f"契约声明 {total_declared} 个端点 · 实现 {total_impl} 个 operation"
          f"（其中 {len(refused)} 个已决议不做）")
    print(f"覆盖率 {total_impl - len(extra)}/{total_declared}"
          f"（实现里有 {len(extra)} 个契约没写）\n")

    if missing:
        print(f"\033[33m声明了但未实现（{len(missing)}）\033[0m")
        for method, path in missing:
            print(f"  {method:6} {path}")

    if refused:
        print(f"\n\033[36m明确不做（{len(refused)}）—— 已决议，不是待办\033[0m")
        for (method, path), note in sorted(refused.items()):
            mark = " ⚠️**已实现（偏离决议）**" if (method, path) in impl else ""
            print(f"  {method:6} {path}  {note}{mark}")

    if refused_but_built:
        print("\n⚠️ 有端点**与决议相反地实现了** —— 要么改契约的决议，"
              "要么删掉实现。别让它悄悄过去。")

    if extra:
        print(f"\n\033[1m实现了但契约没写（{len(extra)}）—— 要么补进契约，"
              f"要么它不该存在\033[0m")
        for method, path in extra:
            print(f"  {method:6} {path}")

    # ---- query 参数面（D151）：契约写了、代码没有 ⇒ 那是**假参数** ----
    declared_q = declared_query_params()
    impl_q = implemented_query_params()
    fake_params: list[tuple[tuple[str, str], list[str]]] = []
    for key, names in sorted(declared_q.items()):
        real = impl_q.get(key)
        if real is None:
            continue          # 端点不存在 —— 上面的 missing 已经报过了
        miss = sorted(names - real)
        if miss:
            fake_params.append((key, miss))
    # 反向只提示不失败：代码多出来的参数不会让按契约写的调用方出错
    undocumented_q = sorted(
        f"{m} {pth} 多了 {sorted(impl_q[(m, pth)] - declared_q.get((m, pth), set()) - GLOBAL_PARAMS)}"
        for (m, pth) in impl_q
        if m in {k[0] for k in declared_q} or (m, pth) in declared_q
    )
    undocumented_q = [x for x in undocumented_q if "多了 []" not in x]

    if fake_params:
        print(f"\n\033[31m契约写了但代码没有的 query 参数（{len(fake_params)}）"
              f"—— 那些参数是假的\033[0m")
        for (method, path), miss in fake_params:
            print(f"  {method:6} {path}  {miss}")
        print("  ⚠️ FastAPI 对**未知 query 参数静默忽略** —— 按契约写的前端"
              "会以为传上了，其实没生效（与 PATCH body 那个坑同形）")

    if undocumented_q:
        print(f"\n\033[33m代码有但契约没写的 query 参数（{len(undocumented_q)}）"
              f"—— 建议补进契约\033[0m")
        for line in undocumented_q:
            print(f"  {line}")

    # 未实现是正常的（分阶段交付），"实现了但没写进契约"不是 ——
    # 那是下一次有人照契约写前端时的陷阱。
    # ⚠️ "与决议相反地实现了"同样算错 —— 决议被绕过比契约漂移更隐蔽。
    # 🔑 假参数也算错：按契约写的调用方会以为传上了 —— 那是**契约骗人**，
    #   和"实现了但没写进契约"同一种危害（§5.142：错描述会让下游白改）。
    bad = extra + refused_but_built + [k for k, _ in fake_params]
    if args.strict and missing:
        bad = missing

    if bad:
        print("\n⚠️ 清单见 docs/DESIGN-API.md 的「明确约定不做」一节")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())