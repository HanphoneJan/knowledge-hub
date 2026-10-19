"""🔑 隐私隔离的单测：**唯一实现** `services/ingest/privacy.py`。

⚠️ **D157 改过导入路径**：这一份原来测的是 `pipeline/privacy/owner_classifier.py`
（一个**纯 re-export shim**，真实现一直在 `services/`）。删掉 v1 遗留目录后
它直接 import `services.ingest.privacy`。
🔑 那条"必须是 re-export、不能是第二份实现"的断言也**顺势变强**了：
从"两个文件 re-export 同一个对象"改成"**整个仓库只有一个定义点**"
（AST 扫 `services/`）—— 后者能抓住"有人在别处又写了一份"。

**为什么它的优先级最高**（`CONVENTIONS.md` §9 排在第 1 位）：

- 归属判定是**隐私红线**的第一道闸门 —— 判错的方向是"他人文档进索引"，
  也就是把他人的简历 / 花名册写进可检索的库里。
- **历史上真实漏判过 3 次**（`PROGRESS.md` §5.5、§5.10），每一次都是先写规则、
  再被新样本推翻。所以它必须有单测，不能只靠 `index/test_owner_diff.py`
  那种"人工看一眼差异"的门禁。

## 测试原则（CONVENTIONS §9）

**覆盖实际踩过的坑，不是理想路径。** 每个用例都对应一个真实漏判 / 误判样本：
§5.5 的漏网文档、§5.10 的两份花名册、§5.10 的滑窗误切（46/96 份误判）。
另有**全量 96 份解析产物**的回归：分类结果必须与 `data/privacy_audit_log.json`
零分歧，且隔离数必须是实测的 25。

⚠️ `data/` 已 gitignore，新克隆的机器上这些用例会 `skip`（而不是假绿）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DOCS_LIGHT = DATA / "docs_light"

# 🔑 直接 import 唯一实现（D157：不再经 pipeline/ 的 re-export shim）
from services.ingest.privacy import (  # noqa: E402
    OTHER_NAMES,
    classify_owner,
    filename_name_candidates,
    roster_names,
)

needs_data = pytest.mark.skipif(
    not DOCS_LIGHT.exists(),
    reason="data/ 已 gitignore（隐私数据不提交），需要先跑解析管线",
)


def _names_configured() -> bool:
    """真实语料回归还要求**本地配置了真实名单**。

    🔑 仓库里只有占位名（`张三`/`李四`…）—— 拿占位名去跑真实语料，分类结果必然与
    审计日志对不上（这不是代码坏了）。真实姓名是**部署方的数据**：写进 `.env` 的
    `PRIVACY_SELF_NAMES` / `PRIVACY_OTHER_NAMES`，跑这两个测试前配上即可。
    """
    from services.common.config import get_settings

    c = get_settings()
    return bool(c.privacy_self_names.strip() and c.privacy_other_names.strip())


needs_names = pytest.mark.skipif(
    not _names_configured(),
    reason="真实语料回归需要 .env 里的 PRIVACY_SELF_NAMES / PRIVACY_OTHER_NAMES"
    "（仓库里只有占位名，见 .env.example）",
)

#: 5 个**名单外**的 3 字姓名（首字都在 `SURNAMES` 百家姓表里）。
#:
#: ⚠️ 两个 fixture 约束，写错就会得到"名单规则没触发"的假失败：
#: 1. 不能拿 `SELF_NAMES` / `OTHER_NAMES` 里的人凑 —— `roster_names` 会跳过它们
#:    （它们由规则 2/5 精确处理）。真实花名册 29 行不受影响，5 行 fixture 会。
#: 2. **首字必须在 `SURNAMES` 里**（`test_姓氏表外的名字不算` 钉住了这条）。
FIVE_STRANGERS = ("郑十", "白崇礼", "慕容云", "聂云台", "岑秋明")


def roster_table(names) -> str:
    return "\n".join(
        f"| {n} | 2023000100{i:03d} | 男 | 汉族 | 共青团员 |"
        for i, n in enumerate(names)
    )


# ============================================================
# 一、单元级：两个辅助函数
# ============================================================


class TestFilenameCandidates:
    """`filename_name_candidates` —— 名单外陌生人的泛化防线（规则 4）。"""

    def test_学号粘连要剥掉(self):
        # 实测漏判样本的形态：学号与姓名粘在一个文件名里。
        # ⚠️ 用**名单外**的姓名：王五已在 OTHER_NAMES 里，由规则 2 精确处理，
        # 走不到规则 4（`filename_name_candidates` 会跳过已知名字）。
        assert filename_name_candidates("20230001000_郑十_企业实习中期报告.docx") == [
            "郑十"
        ]

    def test_已知名单不走泛化(self):
        """规则 4 只处理名单外的人；已知的由规则 0/2 处理（职责不重叠）。"""
        assert filename_name_candidates("20230001002_王五_企业实习答辩.pptx") == []
        assert filename_name_candidates("张三-申请书.docx") == []
        assert filename_name_candidates("张三-示例大学-算法.docx") == []

    @pytest.mark.parametrize(
        "name",
        [
            # 这 3 个是实测全量 105 份文件名后仅剩的误判候选，已进 STOPWORDS
            "张三-AI应用开发-示例大学-27届.docx",
            "项目简介.md",
            "班徽.png",
        ],
    )
    def test_停用词不算人名(self, name):
        assert filename_name_candidates(name) == [], name

    def test_长数字单独成段不算(self):
        assert filename_name_candidates("0904.docx") == []


class TestRosterNames:
    """`roster_names` —— 名单型文档识别（§5.10 的关键教训）。"""

    def test_真名单_按表格单元格提取(self):
        found = roster_names(roster_table(FIVE_STRANGERS))
        assert len(found) == 5, found

    @pytest.mark.parametrize("name", ["皇甫明", "司徒静", "诸葛云"])
    def test_姓氏表外的名字不算(self, name):
        """⚠️ 首字不在 `SURNAMES` 百家姓表里 → 不算人名单元格。

        这条约束是"宁可漏隔离也不误判格式词"的取舍：
        表里只有约 300 个姓，罕见姓氏会漏。
        **但方向是安全的** —— 漏掉一个单元格最多让名单规则少触发一次，
        后面还有规则 2/5 兜底；而放宽成"任意 2~3 字"会把
        《实习总结报告的书写规范》的字体表误判成名单（§5.10 实测）。
        """
        assert roster_names(roster_table((name,))) == []

    def test_已知名单不计入门槛(self):
        """⚠️ 一个容易"顺手修错"的地方：名单里已知的名字**不算**进门槛。

        它们由规则 2/5 精确处理，重复计数没有意义。
        真正的花名册有几十行，不受这个性质影响。
        """
        mixed = ("郑十", "孙七", "赵六", "张三", "王五")
        assert roster_names(roster_table(mixed)) == []  # 只剩 1 个陌生人
        assert roster_names(roster_table(FIVE_STRANGERS)) != []

    @pytest.mark.parametrize(
        "text",
        [
            # ⚠️ §5.10 的核心教训：`[一-鿿]{2,4}` 滑窗会把任意连续汉字
            # 切成重叠片段（"解码器结构" → 解码器结/码器结构/…），
            # 实测把 **46/96 份文档**误判成名单。
            # 散文里连着出现 5 个陌生人的名字**不算**名单。
            "本文讨论了解码器结构与注意力机制，郑十司徒静白崇礼慕容云皇甫明反复出现。",
            # 字体表里的格式词是 4 字，不能当人名
            "| 中文 | 宋体 | 小四 | 加粗 |\n| 编码器结构 | 是 | 否 | 否 |",
        ],
    )
    def test_非名单不误判(self, text):
        assert roster_names(text) == [], roster_names(text)

    @pytest.mark.parametrize("n", [0, 1, 3, 4, 5])
    def test_门槛是_5_个陌生姓名(self, n):
        """`ROSTER_MIN = 5`：4 人是"提到了几个人"，5 人才是"名单"。"""
        assert bool(roster_names(roster_table(FIVE_STRANGERS[:n]))) is (n >= 5)


# ============================================================
# 二、真实漏判样本（PROGRESS §5.5 / §5.10）—— 漏判方向，零容忍
# ============================================================


class TestKnownMisses:
    """这些文档**曾经**被判成 unknown 而进了索引（§5.5 的表）。"""

    @pytest.mark.parametrize(
        "filename",
        [
            # 名单外的陌生人：硬编码名单里没有"王五"
            "20230001002_王五_企业实习答辩_收获页更新版.pptx",
            # 他人自传：正文只出现他人姓名 2 次 —— 不设次数阈值才拦得住
            "自传.md",
        ],
    )
    def test_曾漏判的文档必须判他人(self, filename):
        r = classify_owner("我叫孙七，男，2023 年 9 月入学。\n" * 2, filename)
        assert r["owner"] == "other", f"{filename}: {r}"

    def test_英文简历_全程无中文字也判他人(self):
        """§5.5 的第 4 份漏网：一份全英文简历，中文规则一条都匹配不到。"""
        cv = (
            "PROFESSIONAL SUMMARY\n"
            "EDUCATION\n"
            "Example University\n"
            "SKILLS\nPython, SQL, Machine Learning\n"
        )
        r = classify_owner(cv, "Resume-Jane Doe.docx")
        assert r["owner"] == "other", r

    @needs_data
    def test_真实花名册走名单型分支(self):
        """两份真实花名册（§5.10）必须由**名单型**规则拦下，而不是碰巧被
        "正文含他人姓名"兜住 —— 后者对"全是陌生人"的花名册无效。"""
        # ⚠️ 只按**文件名**在语料里找（`rglob`），不写死目录层级：
        #   语料的目录结构属于**本机数据的组织方式**（按人/按项目分目录），
        #   把它写进公开仓库没有任何信息价值，只会暴露"谁的机器上有什么"。
        #   文件名本身是通用表单名（花名册 / 人员发展表），不含个人信息。
        # ⚠️ `docs_light` 的产物名是 `stem + 原后缀 + .md`，所以磁盘上是两层后缀。
        cases = ["班级成员基本情况.docx", "25年6月发展.xlsx"]
        checked = 0
        for stem in cases:
            hits = sorted(DOCS_LIGHT.rglob(f"{stem}.md"))
            if not hits:
                continue
            checked += 1
            src = hits[0].read_text(encoding="utf-8", errors="ignore")
            r = classify_owner(src, stem + ".docx")
            assert r["owner"] == "other", r
            assert "名单" in r["reason"], f"{stem} 走的是 {r['reason']} 而不是名单型"
        assert checked == len(cases), f"只找到 {checked}/{len(cases)} 份真实花名册"


# ============================================================
# 三、方向性取舍：宁可误隔离，不可漏隔离
# ============================================================


class TestDirectionalBias:
    def test_子串互伤_本人优先(self):
        """§5.5：他人 `自传.docx` 与本人 `张三--自传.docx` 互为子串。

        所以本人判定必须用**文件名**，精确匹配，不能用 `stem in doc`。
        """
        other = classify_owner("我叫孙七。", "自传.docx")
        mine = classify_owner("我叫张三。", "张三--自传.docx")
        assert other["owner"] == "other", other
        assert mine["owner"] == "self", mine

    def test_正文含本人标识不能短路(self):
        """历史漏判：`含本人标识` 提为规则 0 后，两份花名册被翻成 self。

        现在文件名才是规则 0，正文里的本人标识只当弱信号（第 6 步）。
        """
        r = classify_owner(
            "张三 20230001001\n" + roster_table(FIVE_STRANGERS),
            "班级花名册.md",
        )
        assert r["owner"] == "other", r
        assert "名单" in r["reason"], r["reason"]

    def test_正文只出现一次他人姓名也判他人(self):
        """不设次数阈值 —— 设阈值会漏判他人自传（他人姓名只出现 2 次）。"""
        r = classify_owner("指导老师孙七老师 signatures 已附。", "暑期实践.md")
        assert r["owner"] == "other", r

    @pytest.mark.parametrize("n", OTHER_NAMES[:5])
    def test_只出现在文件名里的他人也判他人(self, n):
        assert classify_owner("", f"{n}-某文档.md")["owner"] == "other"


# ============================================================
# 四、边界
# ============================================================


class TestEdges:
    @pytest.mark.parametrize(
        "text, filename, owner",
        [
            ("", "", "unknown"),  # 全空
            ("", "模板.md", "unknown"),
            ("Attention is all you need", "论文笔记.md", "unknown"),
            ("我是张三", "自我介绍.md", "self"),
        ],
    )
    def test_不抛异常且给出稳定判定(self, text, filename, owner):
        r = classify_owner(text, filename)
        assert r["owner"] == owner, r

    def test_三元组齐全(self):
        """审计日志依赖 `{owner, reason, evidence}`（可解释、可追溯，§6.4 M2）。"""
        for r in (classify_owner("", "x.md"), classify_owner("张三", "y.md")):
            assert set(r) == {"owner", "reason", "evidence"}, r
            assert r["reason"], r


# ============================================================
# 五、全量回归：96 份真实解析产物
# ============================================================


def _load_corpus() -> list[dict]:
    """把 `docs_light` 的解析产物与 registry / 审计日志对齐。

    ⚠️ 文件名对不齐的那一步是 §5.9 的修复：`docs_light` 的文件名是
    `stem + 原后缀 + .md`（保留原扩展名，否则同 stem 不同格式会互相覆盖）。
    所以要对齐 registry 得**剥两层**后缀。
    """
    reg = {
        d["path"]: d
        for d in json.loads((DATA / "doc_registry.json").read_text(encoding="utf-8"))
    }
    audit = {
        a["source_path"]: a
        for a in json.loads((DATA / "privacy_audit_log.json").read_text(encoding="utf-8"))
    }
    rows = []
    for md in sorted(DOCS_LIGHT.rglob("*.md")):
        rel = str(md.relative_to(DOCS_LIGHT))
        src = re.sub(r"\.[^.]+$", "", rel[:-3])  # 去 .md，再去原后缀
        rows.append(
            {
                "src": src,
                "filename": md.name,
                "text": md.read_text(encoding="utf-8", errors="ignore"),
                "guess": reg.get(src, {}).get("owner_guess"),
                "audited": audit.get(src, {}).get("owner"),
            }
        )
    return rows


@pytest.fixture(scope="module")
def corpus() -> list[dict]:
    rows = _load_corpus()
    assert len(rows) >= 90, f"只读到 {len(rows)} 份，语料不完整"
    return rows


def classify(row: dict) -> dict:
    return classify_owner(row["text"], row["filename"], row["guess"])


@needs_data
@needs_names
class TestFullCorpus:
    def test_与审计日志零分歧(self, corpus):
        """审计日志是三轮修复后的最终判定，分类器必须复现它。

        这是最强的回归基线：任何规则改动导致的判定变化都会在这里显形，
        逼着你去看清"改了哪几份、为什么"。
        ⚠️ 反面教材是 §5.9：`parser_benchmark.json` 报 96/96 成功，
        产物却只有 90 个（6 份被同 stem 覆盖）—— 聚合数字必须与产物交叉验证。
        """
        diff = [
            (r["src"], r["audited"], classify(r)["owner"])
            for r in corpus
            if r["audited"] and classify(r)["owner"] != r["audited"]
        ]
        assert not diff, "\n".join(f"  {s}: 旧={o} 新={n}" for s, o, n in diff)

    def test_隔离数是实测的_25(self, corpus):
        """25 是归属判定的实测隔离数（硬编码名单时代 23 → 25）。

        ⚠️ 这个数**只允许在有意识地修分类规则时改**，改的时候必须同时更新
        `PROGRESS.md` §5.5 的表并说明为什么 —— 它是"漏了几个"的直接读数。
        """
        owners = [classify(r)["owner"] for r in corpus]
        assert owners.count("other") == 25, (
            f"隔离数变成 {owners.count('other')}（应为 25）。"
            "如果是有意修分类规则，请同时更新 PROGRESS §5.5 并说明理由"
        )

    def test_漏隔离方向为零(self, corpus):
        """🔑 曾经判为他人、现在判不是他人的文档数必须是 0。

        这是本文件最重要的断言：**误隔离可以接受，漏隔离不可以**。
        """
        leaked = [
            r["src"]
            for r in corpus
            if r["audited"] == "other" and classify(r)["owner"] != "other"
        ]
        assert not leaked, leaked

    def test_本人文档不被误隔离(self, corpus):
        """审计里的 41 份 `self` 不该变成 `other`。

        误隔离虽然可接受，但**不该悄悄发生**，这里把它钉住。
        """
        flipped = [
            r["src"]
            for r in corpus
            if r["audited"] == "self" and classify(r)["owner"] == "other"
        ]
        assert not flipped, flipped

    def test_每份都有可解释的理由(self, corpus):
        """审计日志依赖三元组；判他人必须有证据。"""
        for r in corpus:
            out = classify(r)
            assert out["reason"], r["src"]
            if out["owner"] == "other":
                assert out["evidence"], f"{r['src']} 判他人却没有证据"

# ============================================================
# 唯一的实现必须在 services/（CONVENTIONS §0.1）
# ============================================================


def test_owner_classification_has_exactly_one_definition_site():
    """🔑 整个仓库里 `classify_owner` **只有一个定义点**。

    归属判定是**隐私红线**：两份实现漂移的方向就是"漏判他人文档"
    （漏判不报错，只让高敏内容进索引）。D47 要求导入时重跑判定，
    而导入 worker 在 `services/` 里 ⇒ 实现必须住在 `services/ingest/privacy.py`。

    🔑 D157 之前这条断言是"`pipeline/privacy/owner_classifier.py` 必须是
    re-export、不能是第二份实现" —— 那是**两个具体文件**之间的比较，
    能抓住"pipeline 侧变成第二份"，但抓不住"有人在第三个地方写了一份"。
    ⇒ 现在扫全仓库 `services/` 的 AST。
    """
    hits = _owner_definition_sites(ROOT)
    assert hits == ["services/ingest/privacy.py"], (
        f"`classify_owner` 的定义点不止一处：{hits}\n"
        "归属判定是隐私红线，两份实现漂移的方向是「漏判他人文档」—— "
        "而漏判不报错，只让高敏内容进索引"
    )


def _owner_definition_sites(root: Path) -> list[str]:
    """`root/services` 里所有定义 `classify_owner` 的地方（相对 `root`）。

    🔑 抽成纯函数是为了能被**合成输入**测：判据是"这个扫描真的覆盖
    `services/` 的每个子目录"，而第一版把扫描写在测试体里，变异脚本把
    `ROOT / "services"` 改成 `ROOT / "services" / "ingest"` 时**全绿** ——
    真实仓库里 `services/ingest/privacy.py` 正好在被扫的目录内，
    于是漏扫别的子目录这件事**测不出来**。
    """
    import ast

    out: list[str] = []
    for f in sorted((root / "services").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "classify_owner":
                out.append(f.relative_to(root).as_posix())
    return out


def test_owner_definition_scanner_covers_every_services_subdir(tmp_path):
    """🔑 扫描必须覆盖 `services/` 的**每个**子目录（合成输入）。

    ⚠️ 变异 `root / "services"` → `root / "services" / "ingest"`
    在真实仓库上抓不到（真实现恰好就在 `services/ingest/`），
    这条合成输入让它无从遁形。
    """
    for sub in ("api", "common", "ingest", "retrieval", "worker", "mcp"):
        d = tmp_path / "services" / sub
        d.mkdir(parents=True)
        (d / "m.py").write_text(
            "def classify_owner(*a, **k):\n    return {}\n", encoding="utf-8"
        )
    assert len(_owner_definition_sites(tmp_path)) == 6, \
        _owner_definition_sites(tmp_path)


def test_owner_definition_scanner_finds_nothing_in_a_clean_tree(tmp_path):
    """🔑 没有定义就返回空（别把"扫不到"当成"只有一处"）。"""
    (tmp_path / "services" / "common").mkdir(parents=True)
    (tmp_path / "services" / "common" / "auth.py").write_text(
        "classify_owner = 1\n", encoding="utf-8"   # 赋值不是 def ⇒ 不算定义点
    )
    assert _owner_definition_sites(tmp_path) == []


def test_services_copy_is_the_one_under_test():
    """🔑 直接调 `services/ingest/privacy.py` 那份做一次端到端判定。

    ⚠️ D157 之前这条的理由是"本文件其余用例走的是 `pipeline/` 的 re-export，
    所以'实现搬家'这件事本身要显式测一次"。删掉 v1 目录后**整个文件都直接
    import 唯一实现**，这条的理由变成"守住判定规则的三个关键分支"：
    中文文件名认自己 · 英文 CV 靠**正文里的独占大写节标题**触发 ·
    只给文件名时判 `unknown`（这是对的，不是缺陷 —— 见模块 docstring 第 1 条）。
    """
    from services.ingest.privacy import classify_owner as svc_classify

    assert svc_classify("张三 简历", "张三-简历.md")["owner"] == "self"
    # ⚠️ 英文简历那条要**正文里有独占一行的全大写节标题**才触发；
    #   只给文件名会判 unknown（这是对的，不是缺陷 —— 见模块 docstring 第 1 条）
    cv = "EDUCATION\nExample University\nPROFESSIONAL SUMMARY\nengineer"
    assert svc_classify(cv, "Resume-Jane Doe.docx")["owner"] == "other"
    assert svc_classify("正文", "Resume-Jane Doe.docx")["owner"] == "unknown"
