"""真实语料检索评测问题集的**模板**（复制为 `eval/_qset.py` 后使用）。

🔴 **为什么真实的问题集不进仓库**：`_qset.py` 的每条标注都指向 `data/` 下
**真实文档**的相对路径与**逐字原文片段**（常见个人资料、简历、证件号）。
把它提交到公开仓库等于直接泄露原文。所以：
- `eval/_qset.py` 与 `reports/*.json` 都在 `.gitignore` 里，**只留本地**；
- 仓库里只放这份**合成示例**（虚构文档/问题，不含任何真实个人数据）。

## 怎么用

```bash
cp eval/_qset.example.py eval/_qset.py
# 按你自己的 data/ 语料改 golden_file / golden_marker / question
uv run python eval/retrieval_eval.py
```

## 标注口径（与正式版相同）

每条 = `(问题, 类别, 真值文件, 真值片段)`：

- `golden_file`：语料里**相对 `data/` 的路径**（与 `_eval_corpus.build` 写进
  `documents.file_name` 的值一致）。
- `golden_marker`：该文件里**逐字出现**的一个短片段。命中判据 = "取回的 chunk
  里出现了这个片段"；搜不到说明**标注过期了**。
- `category`：`doc` / `blog` / `entity` / `date` / `table` / `chat`。

⚠️ 这里是**虚构示例**：直接跑只会得到无意义的分数，仅用于确认脚本能跑通。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalQuery:
    seed: int
    category: str
    question: str
    golden_file: str
    golden_marker: str
    #: 契约 §5.2 的 answer_type：fact / flow / compare / refuse
    answer_type: str = "fact"


#: 示例问题集（**合成语料**，请替换成你自己的标注）。
QUERIES: list[EvalQuery] = [
    EvalQuery(
        1,
        "doc",
        "安装前需要检查哪些环境依赖",
        "docs_light/demo/部署手册.md",
        "需要 Python 3.13 与 PostgreSQL 16",
        "flow",
    ),
    EvalQuery(
        2,
        "table",
        "服务默认占用哪些端口",
        "docs_light/demo/端口一览表.md",
        "8010",
        "fact",
    ),
    EvalQuery(
        3,
        "entity",
        "编号 SN-2026-0007 对应哪台设备",
        "docs_light/demo/设备清单.md",
        "SN-2026-0007",
        "fact",
    ),
]
