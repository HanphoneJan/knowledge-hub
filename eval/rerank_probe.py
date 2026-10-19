"""🔍 选默认 Reranker：同一组中文 query 对比两个模型。

不是"哪个模型更大"，而是"在**这个项目的语料类型**上哪个判别力好"。
第一版选 `bge-reranker-base`（138M），实测在"表格怎么填"这类中文
query 上把**标题行**排在**答案段**之上（见 D87）—— 必须拿数据说话。
"""

import os
import time

for _k in ("ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["HF_HUB_OFFLINE"] = "1"

MODELS = ["BAAI/bge-reranker-base", "BAAI/bge-reranker-v2-m3"]

#: 样本刻意分两类：① 通用语义（"模型应该会"）② 中文公文/表格填写
#: （本项目语料的主体，也是 base 档翻车的地方）
CASES = [
    ("北京是中国的首都", "北京是中华人民共和国的首都，也是政治文化中心", True),
    ("北京是中国的首都", "红烧肉的做法是先炒糖色再下五花肉", False),
    ("表单那一栏怎么填写",
     "入职日期一栏应填写合同签订当天，格式为年-月-日", True),
    ("表单那一栏怎么填写",
     "本文档为费用收缴明细表", False),
    ("申请书怎么写",
     "申请材料应包含个人简介与项目经历两部分", True),
    ("申请书怎么写",
     "本文档为费用收缴明细表的填写示例", False),
    ("入职日期那栏该填什么",
     "入职日期应填写劳动合同签订当天的日期", True),
    ("入职日期那栏该填什么",
     "本表一式四份，各部门留存一份", False),
]


def main() -> int:
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    print(f"{'query':<24} {'正':<2} {'base':>9} {'v2-m3':>9}  两者")
    print("-" * 62)
    rows = []
    for model in MODELS:
        tok = AutoTokenizer.from_pretrained(model)
        mdl = AutoModelForSequenceClassification.from_pretrained(model)
        mdl.eval()
        scores = []
        t0 = time.perf_counter()
        for q, p, _gold in CASES:
            with __import__("torch").inference_mode():
                # ⚠️ pair 要用 text/text_pair 两组：tok([q, p]) 会被当成
                #   "两条独立序列"而不是"一对"，而且长度不同直接报
                #   "expected sequence of length 7 at dim 1"（踩过）
                b = tok([q], [p[:1200]], return_tensors="pt", truncation=True,
                        max_length=512)
                s = float(mdl(**b).logits.view(-1)[0])
            scores.append(s)
        rows.append((scores, (time.perf_counter() - t0) / len(CASES) * 1000))

    base, base_ms = rows[0]
    v2, v2_ms = rows[1]
    wins_base = wins_v2 = 0
    for i, (q, p, gold) in enumerate(CASES):
        tag = "✓" if base[i] > -base[0] else ""
        print(f"{q[:22]:<24} {'正' if gold else '负':<2} "
              f"{base[i]:>9.2f} {v2[i]:>9.2f}")

    print("-" * 62)
    # 逐 query 看成对排序（正 > 负）才是判别力，而不是看绝对分
    def pair_acc(scores, idx):
        return all(scores[idx] > scores[idx + 1] for idx in range(0, len(idx), 2))

    pair_idx = list(range(0, len(CASES), 2))
    ok_base = sum(1 for i in pair_idx if base[i] > base[i + 1])
    ok_v2 = sum(1 for i in pair_idx if v2[i] > v2[i + 1])
    print(f"成对判别正确数：base {ok_base}/{len(pair_idx)} · "
          f"v2-m3 {ok_v2}/{len(pair_idx)}")
    print(f"单对耗时：base {base_ms:.0f}ms · v2-m3 {v2_ms:.0f}ms")
    if ok_v2 > ok_base:
        wins_v2 = 1
    elif ok_base > ok_v2:
        wins_base = 1
    print(f"→ 判别力更好的是：{'bge-reranker-base' if wins_base else 'bge-reranker-v2-m3' if wins_v2 else '打平'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())