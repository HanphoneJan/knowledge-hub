"""证据质量判定（文章《RAG 优化》§常见错误「忽略拒答能力」）。

## 为什么需要它

文章要求：**"检索结果置信度低、证据相互矛盾，或用户无权访问关键文档时，系统
应拒答、追问或升级人工。可在检索后增加证据质量判断"**。

本项目此前的拒答只覆盖**最极端**的一档：抽取式生成器"一句都摘不出来"时如实说
"没有能回答的证据"。但还有一档更隐蔽：**证据存在、却只来自一条检索腿**（没有任何
跨腿共识）—— 那时排序是单腿的，可信度明显更低，而 LLM 生成器很容易拿它编出一段
流畅的答案。

## 判据：用 RRF 自己的信号，不加新调用

`FusedHit.legs` 就是"这个 chunk 被哪几条腿召回"。跨腿共识（`len(legs) >= 2`）
正是 RRF 想要的性质（`fusion.py` docstring：两条腿都认可 > 一条腿排第一）。
所以这里**只读已有字段**，不额外查库、不调模型 —— 纯函数，可单测。

| 档位 | 条件 | 含义 / 处置 |
|---|---|---|
| `none` | 没有任何命中 | 硬拒答：没有证据可依据 |
| `weak` | 有命中，但**最强证据只来自一条腿** | 降级提示：答案是单腿排序的结果 |
| `ok` | 最强证据有 ≥2 条腿认可 | 正常 |

⚠️ 这三档不改变抽取式生成器的行为（它自己已经会在摘不出句子时拒答）；
它的用途是**给 LLM 生成器一个可依赖的"证据不足"信号**，以及给调试面板一个可读结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Level = Literal["ok", "weak", "none"]


@dataclass(frozen=True)
class EvidenceQuality:
    level: Level
    reason: str
    #: 最强证据被几条腿召回（跨腿共识强度）。`none` 时为 0。
    max_legs: int = 0

    @property
    def weak(self) -> bool:
        return self.level != "ok"


def assess(hits) -> EvidenceQuality:
    """`list[FusedHit]` → 证据质量。**只看已有的 `legs` 字段**（见模块 docstring）。"""
    items = list(hits or [])
    if not items:
        return EvidenceQuality("none", "没有检索到任何证据", 0)
    # 头部若干条的共识强度 —— 只看最靠前的，避免被长尾的单腿命中冲淡
    head = items[:5]
    max_legs = max((len(getattr(h, "legs", []) or []) for h in head), default=0)
    if max_legs >= 2:
        return EvidenceQuality("ok", f"头部证据有 {max_legs} 条腿一致", max_legs)
    return EvidenceQuality(
        "weak", "头部证据只来自一条检索腿，可信度较低", max_legs or 1
    )


__all__ = ["EvidenceQuality", "Level", "assess"]
