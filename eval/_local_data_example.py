"""本机验收探针的**合成示例**（复制为 `eval/_local_data.py` 后填真实数据）。

## 为什么要有这一层

`api_1c` / `api_1d` / `scan_recheck` 要在**真实的表单 PDF**上验"能检索到真值"。
那类文档是**部署方的真实数据**（个人材料），所以：

- 仓库里**只有**这份合成的示例（`_local_data_example.py`）；
- 真值放在 `eval/_local_data.py` —— 它已 `.gitignore`，**只留本地**；
- 脚本优先读 `_local_data`，没有就退回这份示例（结构示意，分数无意义）。

```bash
cp eval/_local_data_example.py eval/_local_data.py
# 按你自己的 PKH_PROBE_PDF 填 PROBE_PDF / QUESTION / ANSWER_MARKERS / SCAN_QUERIES
```

🔑 **测试/验收文件里不许出现真实信息**：一切"真实值"都从这里（不被追踪）读。
守卫见 `tests/test_no_real_data.py`（它按一份本地 deny-list 扫描已跟踪文件）。
"""

from __future__ import annotations

#: 探针 PDF（相对仓库根或绝对路径）；也可用环境变量 `PKH_PROBE_PDF` 覆盖。
PROBE_PDF = "data/probe/示例表单.pdf"

#: 验收问题（答案应在 PDF 的某一页）。
QUESTION = "入职日期那栏填什么"

#: 命中 chunk 必须包含的原文标记（用于判定"检索到了真值"，取任一即可）。
ANSWER_MARKERS: tuple[str, ...] = ("入职日期", "栏内")

#: `QUESTION` 的一个近义变体（精排那一节用它，避免与主问题逐字相同）。
QUESTION_VARIANT = "入职日期那栏该填什么"

#: 语义腿探针（给 `mode=semantic` 用）。
SEMANTIC_PROBE = "入职培训"

#: `mode=exact` 的用例：必须是**正文里确实存在的连续子串**。
EXACT_Q = "按合同签订日填写"

#: 第三轮指代追问应能并上的上一轮问句片段。
PREV_QUESTION_MARKER = "变更流程"

#: 扫描件重测（`scan_recheck.py`）用的问题集。
SCAN_QUERIES: tuple[str, ...] = (
    "扫描件里入职日期那栏填什么",
    "这份扫描版里关于变更的内容",
    "影印件第二页写了什么",
    "扫描版里评审会的决议",
)
