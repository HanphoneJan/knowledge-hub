"""📄 `README.md` 与入口文档的**指向有效性**守卫。

## 为什么需要它

2026-10-05 查 `service/` 是不是死代码时，发现**根 `README.md` 教的是 v1 的启动
方式**（`uvicorn service.app:app --port 8002`）—— 照它做会**起错服务**。
重写之后又立刻犯了两处同类错：

| 我写错的 | 怎么发现的 |
|---|---|
| `dev.sh status` | `dev.sh` 的 `case` 只有 `start` / `restart` / `stop` —— 根本没有这个分支 |
| `services/ingest/parsers/`、`retrieval/hybrid.py` | `parsers.py` 是**文件**不是目录；融合叫 `fusion.py` 不叫 `hybrid.py` |

🔑 通则：**文档里出现的每一条"可执行的指令"与"路径"，都要能在仓库里对上**。
这类错**不会让任何测试变红** —— 文档不是代码，pytest 管不到它；
而一个指错的路径会让人在**排查与被测物无关的问题**上浪费时间（§5.131）。

## 四条判据

1. README 里提到的**每个仓库路径都存在**
2. README 提到的 `dev.sh` **子命令真的存在**
3. README 的**启动端口/入口**与 `deploy/dev.sh` 里的一致
4. 入口文档的**基线数字**与实际测试数一致（pytest 项数）

⚠️ **本守卫的盲区**（写下来是为了下一个人知道边界）：
- 它只查**路径与命令**是否存在，查不了**命令的语义**对不对
  （比如某个参数名写反了）。
- 第 4 条只盯 pytest 一项；eval / e2e 的项数在 `.docs/MEMORY.md` 里，
  变了要手动同步（§5.137：数字漂移是这一类文档最常见的病）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DEV_SH = ROOT / "deploy" / "dev.sh"

#: 入口文档：它们必须先读，所以指向错会更早坑到人
#: 入口文档：**只有公开的那一份一定在仓库里**（CI 上就守它）。
#: 内部交接文档（HANDOFF / MEMORY / PLAN）已移到 `.docs/`、**不进仓库** ⇒ 本地在就一起守、
#: 不在就跳过 —— ⚠️ 跳过只许因为"这份文件不在这个 checkout 里"，不许因为"我没搜到"（§5.131 同族）。
ENTRY_DOCS = ("README.md",)
_LOCAL_ENTRY = (".docs/HANDOFF.md", ".docs/MEMORY.md", ".docs/PLAN.md")


def _entry_docs(root: Path) -> tuple[str, ...]:
    return tuple(ENTRY_DOCS) + tuple(n for n in _LOCAL_ENTRY if (root / n).exists())


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================
# 1. README 里的路径都存在
# ============================================================

#: ⚠️ 一个**块**里出现这些词，就说明它是在**说这个东西被删了**，
#: 而不是在**指向它**。
#:
#: 判"某个路径必须存在"时最容易自己打自己：README 开头那句
#: "⚠️ D157：那套 v1 代码已**删除** —— `service/` `index/` …"
#: **故意**列出被删的目录，而守卫把它判成"指向不存在的路径"。
#: 这是 §5.150b 的姊妹坑：**判"不该出现 X"之前，先确认 X 不会出现在
#: "说明 X 不该出现"的地方**（警告、归档链接、历史记录）。
_DELETED_MARKERS = ("已删除", "已删", "D157", "~~", "曾经", "v1")


def _blocks(text: str) -> list[list[str]]:
    """把文本切成**块**（连续的非空行；空行断开）。

    🔑 为什么按块而不是按行：那句 D157 警告是**跨行**的 ——

    ```
    > ⚠️ **D157**：那套 v1 代码已**删除** —— `service/` `index/` `pipeline/`
    > 三个顶层目录、**`eval/retrieval_eval.py`**、`eval/privacy_scan.py`
    ```

    "已删除"在**第一行**，而两个路径在**第二行**。逐行判断时第二行没有标记
    ⇒ 守卫把它们判成"指向不存在的路径"（我写这条规则的第一版就这样）。
    ⇒ 按块判断：块里**任一行**有标记，整块跳过。
    """
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        if line.strip():
            cur.append(line)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def _mentions_deleted(block: list[str]) -> bool:
    """这个块是不是在**说什么东西已被删 / 还没建**（而不是指向一个现有资源）。

    ⚠️ 名字沿用旧版，实际判据是"这行不是**指向**"。
    两种该放过的情形：
    ① **已删** —— 警告/历史里提到被删的路径（§5.150b）
    ② **待建** —— 施工图里提到还不存在的文件（`.docs/DESIGN-HISTORY.md §1`）
    """
    joined = " ".join(block)
    return any(m in joined for m in _DELETED_MARKERS)


def _tree_paths(text: str) -> list[frozenset[str]]:
    """从 README 的**目录树**里取出路径（带父级上下文）。

    🔑 为什么必须带父级：那段树是**两级**的 ——

    ```
    services/
      ingest/       parsers.py（解析器派发表）/ chunker / versions
    ```

    缩进行里的 `ingest/` 只是**相对父目录**的名字，而真正的路径是
    `services/ingest/`。⚠️ 我第一版直接把缩进行当独立路径，于是
    `ingest`（去掉尾斜杠后不含 `/`）被丢掉 ⇒ **一个都没匹配上**，
    变异脚本把 `parsers.py` 改成 `parsers/` 时全绿。

    ⇒ 这里维护"当前父目录"，并把 `名字`、`名字.py`、`名字/` 三种写法都试一遍
    （README 里两种都出现了：`parsers.py` 与 `purge`）。
    """
    out: list[frozenset[str]] = []
    parent = ""
    for block in _blocks(text):
        if _mentions_deleted(block):
            continue
        for line in block:
            # 顶层：`services/`、`deploy/`（行首无缩进、以 `/` 结尾或带说明）
            m_top = re.match(r"^([a-z_]+)/\s*(#.*)?$", line)
            if m_top:
                parent = m_top.group(1)
                out.append(frozenset({parent}))
                continue
            if not line.startswith((" ", "\t")) or not parent:
                continue
            m_sub = re.match(r"^\s+([a-z_]+)/\s+(.*)$", line)
            if not m_sub:
                continue
            sub = m_sub.group(1)
            out.append(frozenset({f"{parent}/{sub}"}))
            # 余下是"空格对齐的模块名列表"，可能有中文说明插在中间
            rest = m_sub.group(2)
            # ⚠️ 中文说明里也可能带 `/`（`FastAPI：routers/（端点）+ main.py`），
            #   而那些词不是模块名 ⇒ 先把 `说明：` 之前的部分砍掉。
            head = re.split(r"[：:]", rest, maxsplit=1)[0]
            # 同理，只取**小写/中划线**开头的词：说明文字里的 `FastAPI`、`MCP`
            #   这种大写词不是文件名（真模块名全小写，这是本仓库的约定）。
            for item in head.split("/"):
                name = re.match(r"\s*([a-z][A-Za-z0-9_.-]*)", item)
                if not name:
                    continue
                n = name.group(1)
                # 🔑 **只在 README 没写扩展名时**才回退到 `.py`。
                #   两条理由来自两个方向：
                #   ① README 写 `auth`（无扩展名）而真实文件是 `auth.py`
                #      ⇒ 要回退，否则满屏假缺口（第一版就是那样，18 个）。
                #   ② README 写 `parsers.py`（有扩展名）时**不许**回退 ——
                #      否则把它改成 `parsers/`（目录不存在）仍会绿，
                #      因为回退目标 `parsers.py` 恰好存在（变异脚本抓到的）。
                alts = {f"{parent}/{sub}/{n}"}
                if "." not in n:
                    alts.add(f"{parent}/{sub}/{n}.py")
                out.append(frozenset(alts))
            # `handlers/（…）` 这种带尾斜杠的目录项
            if re.search(r"[a-z_]+/\s*[（(]", head):
                out.append(frozenset({f"{parent}/{sub}"}))
    return out


def _backtick_paths(text: str) -> set[str]:
    """正文里反引号包着的路径（含斜杠的才算）。

    ⚠️ 跳过那些"在说它已被删"的**块**（见 `_DELETED_MARKERS` 与 `_blocks`）。
    """
    out = set()
    for block in _blocks(text):
        if _mentions_deleted(block):
            continue
        for line in block:
            for tok in re.findall(
                r"`([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+/?)`", line
            ):
                out.add(tok.rstrip("/"))
    return out


#: 🔑 除了 README，**当前开发任务的设计文档**也要守 ——
#: 新 AI 会照着它改代码，路径打错要白查很久（§5.131）。
#: 为什么不是"所有文档"：历史/归档文档里提到旧路径是**应该**的。
#: 会提到"路径"的活跃文档。⚠️ 后两份是**内部**设计文档（在 `.docs/`、不进仓库）⇒ 本地才守。
DOCS_WITH_LIVE_PATHS = (README,) + tuple(
    p
    for p in (ROOT / ".docs" / "DESIGN-HISTORY.md", ROOT / ".docs" / "DESIGN-MODULES.md")
    if p.exists()
)

#: **还没建**的文件（`.docs/DESIGN-HISTORY.md §1` 是施工图，必然提到它们）。
#: 🔑 显式列出而不是靠"这一行写着'新增'"来推断 ——
#: 后者会漏（设计文档里写的是 `**新**` 而不是"新增"）也会误放（"最新"里也有"新"）。
#: **文件真的建出来之后，从这份清单里删掉它** —— 那一行就是它的"已转正"标记。
# ⚠️ D158 的 `services/visual_service/main.py` 与 `tests/test_visual_service.py`
#   **已建好，已从这份清单里删掉**（`test_planned_paths_are_still_missing`
#   就是为了让白名单不变成垃圾桶 —— §5.137）。
#   下一件待建的东西应加在这里。
PLANNED_PATHS = frozenset()

#: 🔑 **故意不在仓库里**的路径（前缀匹配）—— 文档提到它们**不算**错路径。
#:
#: 为什么需要它：仓库里有两类"文档里说得对、但 checkout 里找不到"的路径，
#: 而它们都是**设计如此**：
#:   1. `.docs/`   —— 内部开发文档（交接/决策/踩坑）。写给维护者、不进仓库；
#:                    公开读者打不开是**预期**，不是死链。
#:   2. `web/dist` —— 前端构建产物（`npm run build` 生成，被 gitignore）。
#:
#: ⚠️ 判据是"这个前缀属于**已知的非仓库区**"，**不是**"找不到就放过" ——
#: 后者会把真正的错路径一起放过（§5.114：判据要指向性质，不是指向"当前结果"）。
#: 也就是说：往这里加一项，必须能说清"它为什么**本该**不在仓库里"。
NOT_IN_REPO_PREFIXES = (
    ".docs/",      # 内部文档
    "web/dist",    # 前端构建产物
)

#: 收尾用的斜杠版本，便于同时匹配"文件"与"目录"写法（`web/dist` vs `web/dist/`）。
_NOT_IN_REPO = tuple(NOT_IN_REPO_PREFIXES)


def _is_local_only(path: str) -> bool:
    """这个路径是不是"故意不进仓库"的那两类之一？"""
    p = path.rstrip("/")
    return any(p == pre.rstrip("/") or p.startswith(pre) for pre in _NOT_IN_REPO)


def _live_path_candidates(doc: Path) -> list[frozenset[str]]:
    """文档里"看起来是仓库路径"的候选项（每项是一组同义写法）。

    ⚠️ **只取以顶层目录开头的**：`retrieval/pool.py` 这种相对写法不看 ——
    猜测它相对哪个目录本身就是不确定的，而设计文档里应该写全路径
    （`services/retrieval/pool.py`），新 AI 才能直接 grep。
    """
    text = _read(doc)
    tops = {p.name + "/" for p in ROOT.iterdir() if p.is_dir()}
    out: list[frozenset[str]] = []
    for blk in _tree_paths(text) + [{x} for x in _backtick_paths(text)]:
        alts = frozenset(
            x for x in blk
            if not ("*" in x or "{" in x or x.startswith(("http", "~")))
            and any(x.startswith(t) for t in tops)
        )
        if alts:
            out.append(alts)
    return out


def test_every_path_in_the_live_docs_exists():
    """🔑 活跃文档里每个"看起来是路径"的 token 都真实存在（待建的除外）。

    ⚠️ 第一版有三个错（都是我自己踩的）：
    ① 把目录树里的 `services/common/auth` 与 `auth.py` 当成**两个都要存在**
       ⇒ 满屏假缺口（真实写法是省略 `.py`）。⇒ 判"候选组里**任一**存在"。
    ② 拿 `retrieval/pool.py` 这种相对写法去根目录找 ⇒ 报不存在。
       ⇒ 只查以顶层目录开头的；设计文档里应写全路径。
    ③ 待建文件（施工图必然提到）被判成缺口 ⇒ 显式 `PLANNED_PATHS` 白名单。
    """
    bad: list[str] = []
    for doc in DOCS_WITH_LIVE_PATHS:
        for grp in _live_path_candidates(doc):
            if grp & PLANNED_PATHS:
                continue
            if all(_is_local_only(a) for a in grp):
                continue
            if not any((ROOT / a).exists() for a in grp):
                bad.append(f"{doc.name}: {' 或 '.join(sorted(grp))}")
    assert not bad, (
        f"活跃文档指向了不存在的路径：{bad}\n"
        "⚠️ 文档里的错路径不会让任何测试变红，而它会让人排查"
        "**与被测物无关**的问题（§5.131）\n"
        "（若它确实是**待建**的，加进 `PLANNED_PATHS` 并从那里删掉旧条目）"
    )


def _stale_planned(paths, root: Path) -> list[str]:
    """`paths` 里**已经存在**的那些（⇒ 该从待建清单里删掉了）。

    🔑 抽成纯函数才能被合成输入测 —— 判据写在测试体里时，
    变异把 `stale = sorted(...)` 换成 `stale = []` 照样全绿
    （守卫安静地报告"清单是干净的"，而它压根没看，§5.151c）。
    """
    return sorted(x for x in paths if (root / x).exists())


def test_local_only_exemption_is_narrow():
    """🔑 例外清单必须**窄**：它只该覆盖"故意不进仓库"的两类。

    变异场景：有人为了让一条死链过关，把 `docs/` 也塞进 `NOT_IN_REPO_PREFIXES`
    —— 那之后所有 `docs/*.md` 的死链都免检了。这条守卫就是拦它的。
    """
    assert _is_local_only(".docs/PROGRESS.md")
    assert _is_local_only("web/dist")
    assert _is_local_only("web/dist/"), "带斜杠的写法也要认（目录写法）"
    for must_check in ("docs/DESIGN-API.md", "docs/architecture.md", "README.md",
                       "services/api/main.py", "web/src/main.tsx", ".env.example"):
        assert not _is_local_only(must_check), (
            f"{must_check} 被豁免了 —— 例外清单只许装「故意不进仓库」的路径"
        )


def test_planned_paths_are_still_missing():
    """🔑 `PLANNED_PATHS` 里的文件**还没建** —— 建好了就把它从清单里删掉。

    🔑 这一条是为了让"白名单不会变成垃圾桶"：白名单里躺着一个**已经存在**的
    文件，就等于给了一条永久的免检通道，而没人会记得去清它（§5.137）。
    """
    stale = _stale_planned(PLANNED_PATHS, ROOT)
    assert not stale, (
        f"这些文件已经建好了，请从 `PLANNED_PATHS` 里删掉：{stale}\n"
        "留着等于给它们一条永久免检通道"
    )


def test_stale_planned_detector_finds_a_file_that_got_built(tmp_path):
    """🔑 "已建好"的判定必须真的看文件系统（合成输入）。"""
    (tmp_path / "services" / "retrieval").mkdir(parents=True)
    (tmp_path / "services" / "retrieval" / "fusion.py").write_text("x")
    paths = ["services/retrieval/fusion.py", "services/visual_service/main.py"]
    assert _stale_planned(paths, tmp_path) == ["services/retrieval/fusion.py"]


def test_every_path_mentioned_in_readme_exists():
    """🔑 README 里每个"看起来是路径"的 token 都真实存在。

    ⚠️ 这条守卫的第一版**抓不到真正的错**，而且是被自己的变异脚本发现的：
    把 `parsers.py`（文件）写成 `parsers/`（目录）时它**全绿** —— 因为
    那时候它只扫反引号，而目录结构那段用的是**空格对齐的注释行**，
    一个路径都没匹配到。
    ⇒ 现在两个来源都扫：目录树（带父级上下文）+ 正文反引号。

    ⚠️ 排除含 `*` / `{` 的（glob）与 `http` / `~` 开头的（URL 与家目录）。
    """
    text = _read(README)
    groups = _tree_paths(text) + [{x} for x in _backtick_paths(text)]
    missing = []
    for grp in groups:
        alts = sorted(x for x in grp
                      if not ("*" in x or "{" in x or x.startswith(("http", "~")))
                      and not _is_local_only(x))
        if not alts:
            continue
        if not any((ROOT / a).exists() for a in alts):
            missing.append(" 或 ".join(alts))
    assert not missing, (
        f"README 指向了不存在的路径：{missing}\n"
        "⚠️ 文档里的错路径不会让任何测试变红，而它会让人排查"
        "**与被测物无关**的问题（§5.131）"
    )


def test_deleted_marker_is_recognised_anywhere_in_the_block():
    """🔑 "这个块在说某东西被删了"要按**整块**判，不是只看首行。

    ⚠️ 判据用**合成输入**：README 里那句 D157 警告的标记恰好在**首行**，
    于是变异把 `_mentions_deleted(block)` 改成 `_mentions_deleted([block[0]])`
    在真实 README 上照样全绿 —— 而那正是这条规则存在的理由（标记常在句首，
    被提到的路径在句尾的续行里）。
    """
    # ⚠️ 路径必须写成 `目录/文件` 那种**斜杠后面还有东西**的形式：
    # 抽路径的正则要求斜杠两侧都有名字，所以 `` `pipeline/` ``（斜杠只在末尾）
    # **根本不会被抽出来** ⇒ 我第一版的合成输入里断言恒成立，
    # 变异脚本把本函数改成按首行判断时照样全绿（又一次"判据落空"）。
    text = (
        "看这些目录：`pipeline/legacy.py` 与 `index/hybrid.py`\n"
        "它们已在 D157 全部删除（3100+ 行）\n"
        "\n"
        "`services/api/main.py`\n"
    )
    dropped = _backtick_paths(text)
    assert "pipeline/legacy.py" not in dropped, dropped
    assert "index/hybrid.py" not in dropped, dropped
    assert "services/api/main.py" in dropped, dropped


def test_readme_does_not_point_at_the_v1_entry_point():
    """🔑 README 不得再教 v1 的启动方式。

    🔴 上一版 README 写的是 `uvicorn service.app:app --port 8002`，
    照它做会起错服务（现行是 `deploy/dev.sh start` → 8010 +
    `services/api/main.py`）。旧正文已随归档删除（D157 之后文档精简）。
    """
    src = _read(README)
    # ⚠️ 只查**可复制的代码块**里有没有它 —— 不能全文搜：新 README 开头那句
    #   警告里**故意**提到了 `uvicorn service.app:app`（说明"别用这个"），
    #   而第一版守卫全文搜，于是把那句警告本身判成了违规（§5.137 的自己打自己）。
    blocks = re.findall(r"```bash(.*?)```", src, re.S)
    assert blocks, "README 里没有 bash 代码块（启动方式应该在那里）"
    assert not any("service.app:app" in b for b in blocks), (
        "README 的命令块里还在教 v1 的启动方式（service.app:app）"
    )
    assert any("deploy/dev.sh start" in b for b in blocks), (
        "README 的命令块里没有给出当前的启动命令"
    )
    # 端口同理：警告里可以提 8002，但命令块里不能
    assert not any("8002" in b for b in blocks), "README 的命令块里还有旧端口 8002"


# ============================================================
# 2. dev.sh 的子命令都存在
# ============================================================

def test_dev_sh_subcommands_mentioned_in_readme_exist():
    """🔑 README 提到的 `dev.sh` 子命令**真的存在**。

    🔴 第一版新 README 写了 `dev.sh status` —— 而那个脚本的 `case` 只有
    `start` / `restart` / `stop`。⚠️ 这是"写文档时按印象补一个看起来该有的命令"
    的典型：写的人不会去翻那个 `case`。
    """
    dev = _read(DEV_SH)
    m = re.search(r"case \"\$\{1:-(\w+)\}\" in(.*?)\nesac", dev, re.S)
    assert m, "dev.sh 的 case 结构变了（守卫要跟着改）"
    body = m.group(2)
    known = set()
    for line in body.splitlines():
        mm = re.match(r"\s*([a-z|]+)\)\s*$", line)
        if mm:
            known |= set(mm.group(1).split("|"))

    used = set(re.findall(r"deploy/dev\.sh\s+([a-z]+)", _read(README)))
    unknown = sorted(used - known)
    assert not unknown, (
        f"README 用了 dev.sh 不支持的子命令 {unknown}；"
        f"实际支持的是 {sorted(known)}"
    )
    # 🔑 反向也钉住：README 用到的**每一个**子命令都要有 case 分支
    #   （`dev.sh start` 会命中 `restart|start)` 那条，所以按"分支里出现过该词"判）。
    for cmd in sorted(used):
        assert re.search(rf"^\s*[a-z|]*\b{cmd}\b[a-z|]*\)\s*$", body, re.M), (
            f"README 让你跑 `dev.sh {cmd}`，但那个 case 分支不在脚本里"
        )


# ============================================================
# 3. 端口与入口一致
# ============================================================

def test_readme_port_matches_dev_sh():
    """🔑 README 说的端口 = `dev.sh` 里 `PORT=` 的值。

    ⚠️ 上一版 README 写 8002（v1 的端口），而 dev.sh 用 8010 ——
    两处都对不上时，人会去 curl 一个没人监听的端口。
    """
    dev = _read(DEV_SH)
    # ⚠️ `PORT="${PKH_API_PORT:-8010}"` —— 真实赋值带**环境变量兜底**，
    #   而第一版守卫只匹配 `^PORT=(\d+)` ⇒ 找不到（报"dev.sh 里没有 PORT"）。
    #   ⇒ 匹配"兜底里的那个数字"，那才是本机的实际端口。
    m = re.search(r'^PORT="\$\{PKH_API_PORT:-(\d+)\}"', dev, re.M) \
        or re.search(r"^PORT=(\d+)", dev, re.M)
    assert m, "dev.sh 里找不到 PORT 赋值（格式变了？守卫要跟着改）"
    port = m.group(1)
    src = _read(README)
    blocks = re.findall(r"```bash(.*?)```", src, re.S)
    assert any(f":{port}" in b for b in blocks), (
        f"README 的命令块里没有 :{port}（dev.sh 用的端口）"
    )
    # ⚠️ 旧端口**可以**出现在开头的警告里（那里正是在说"别用这个"），
    #   所以同样只查命令块 —— 第一版全文搜 `8002`，把那句警告判成了违规。
    assert not any("8002" in b for b in blocks), (
        "README 的命令块里还有旧端口 8002（v1 用的那个）"
    )


# ============================================================
# 4. 入口文档的 pytest 项数
# ============================================================

def test_entry_docs_state_the_current_pytest_count():
    """🔑 入口文档里的 pytest 项数 = **实测收集数**（`--collect-only`）。

    ⚠️ 为什么是"收集数"而不是 `pytest -q` 末行那个数：
    `pytest -q` 打印的是 `N passed, 1 skipped` —— 那是**执行结果**，
    而文档里那句"pytest N 项"说的是**有多少条测试**。
    两者在有 skip 时**差 1** ⇒ 用哪个都能对，只要文档与判据一致。
    🔴 **这条守卫第一次跑就抓到了我自己**：我照着 `pytest -q` 的输出写了 1072，
    而它数到 1073（1072 passed + 1 skipped）⇒ 基线里 pytest 段红了一条。
    这正是它该做的事（§5.137 那一族），所以判据与文档现在都以**收集数**为准。

    ⚠️ 只跑 `--collect-only`（不执行）：整个套件要 40s，而这条只需要"有多少个"。
    """
    p = subprocess.run(
        ["uv", "run", "pytest", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    m = re.search(r"(\d+) tests? collected", p.stdout)
    assert m, f"数不出 pytest 项数：{p.stdout[-200:]}"
    real = int(m.group(1))

    stale = []
    for name in _entry_docs(ROOT):
        src = _read(ROOT / name)
        for mm in re.finditer(r"pytest\s*[*#]*\s*(\d{3,4})", src):
            if int(mm.group(1)) != real:
                stale.append(f"{name}: {mm.group(1)}")
    assert not stale, (
        f"实测 pytest 收集到 {real} 项，但入口文档写着别的数字：{stale}\n"
        "⚠️ 数字漂移是这一类文档最常见的病（§5.137）—— 改完测试记得同步"
    )
