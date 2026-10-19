"""归属判定（owner_classifier）—— 隐私隔离的第一道闸门。

## 为什么这个文件在 `services/ingest/` 而不是 `pipeline/privacy/`

CONVENTIONS §0.1：`services/` **不得** import `pipeline/` / `index/`；
而"既被离线脚本用、又被运行时用"的逻辑要把**被调用方**放进 `services/`，
`pipeline/` 只作为 CLI 包装调它。

D47 要求"导入时重跑归属判定"，而 `import/chat` 的 worker 在 `services/` 里 ——
不放进来就只能 (a) 违反 §0.1，或者 (b) 起子进程跑外部脚本（1740 个切片
= 1740 次进程启动，且运行时依赖 `pipeline/` 目录存在）。
所以实现搬到这里，`pipeline/privacy/owner_classifier.py` 变成 re-export 包装
（有测试钉住两者是**同一批对象**，不许各自漂移）。

## 判定逻辑本身（逐条都有实测依据，**不要顺手改**）

为什么不能只靠硬编码人名清单（v2 之前的做法）：
  写死的名单**必然漏人**。实测漏过一个不在名单里的名字 ⇒ 判成 unknown
  ⇒ 未隔离 ⇒ 他人文档进了索引与时间线，违反隐私红线。
  本模块改为**规则式**：从文件名分段里识别「姓 + 1~3 字名」的候选人，
  配合停用词表排除「毕业设计」这类误判；名单只作为补充信号。

判定优先级（关键取舍，务必读）：
  文件名里出现本人名字 → 归属明确是本人，优先判 `self`；
  其余情况下只要**显式他人姓名出现一次即判 other**——不设次数阈值。
  理由（实测）：设阈值会漏判两类高敏文档
    「某人的自传」= 正文只出现本人名字 2 次
    「成员花名册」= 表格里列着他人的学号/出生日期/民族
  误伤方向（把本人文档误判为他人→少索引几条）可接受，漏判方向不可接受。

  0. 文件名含本人标识            → self（最先短路：本人简历也是英文排版）
  0.5 名单型文档                 → other（多人名册，含本人行也算，见 roster_names）
  1. 英文简历特征 + 人名形态文件名 → other
  2. 文件名含显式他人姓名         → other
  3. registry owner_guess=other  → other
  4. 文件名含「姓+名」候选        → other（泛化，防名单外的陌生人）
  5. 正文含显式他人姓名           → other（不设阈值，见上）
  6. 正文含本人标识               → self
  7. 否则                        → unknown（模板/技术资料）

## ⚠️ 聊天切片不能拿这个判定当门禁（D91）

实测：聊天导出的**每一个** 50 条切片都判 `other`
（理由"正文含他人姓名"）—— 因为聊天必然出现对话方的名字。
拿它当门禁会把整个聊天库导空。所以导入任务**记录**判定、不**拦截**
（见 `handle_import_chat`）。

为什么需要第 1 条（英文简历）：
  形如 `简历/Resume-Jane Doe.docx` 的英文简历含电话与邮箱，
  但**全文没有一个中文字**能被第 2/4/5 条匹配到，实测被判成 unknown 进了索引。
  特征 = 正文有独占一行的全大写节标题（EDUCATION / PROFESSIONAL SUMMARY…）
        + 文件名呈人名形态（Resume/CV，或两个以上英文单词）。
"""

from __future__ import annotations

import re
from pathlib import Path

# ── 名字表：仓库里**只留占位名**，真实值从配置读 ───────────────────────
# 🔑 为什么改成配置（而不是写死）：① 名单里是**真人姓名**（本人 + 同学/同事），
#   写进公开代码等于把第三方个人信息一起发布；② 名单本来就随人变 ——
#   换一批语料就要改代码，而它其实是**部署方的数据**，不是程序的常量。
# 配置见 `services/common/config.py` 的 `privacy_self_names` / `privacy_other_names`
# （`.env` 里写 `PRIVACY_SELF_NAMES=…`，逗号分隔），未配置时用下面的占位名。
DEFAULT_SELF_NAMES = ["张三", "zhangsan"]
DEFAULT_OTHER_NAMES = ["李四", "王五", "赵六", "孙七", "周八"]

#: 生效值（import 时从配置算一次；`reload_names()` 可重算，供测试与 CLI 用）
SELF_NAMES: list[str] = []
OTHER_NAMES: list[str] = []


def _split_names(raw: str) -> list[str]:
    """`"张三, 李四"` → `["张三", "李四"]`（去空白、去空项、保序去重）。"""
    out: list[str] = []
    for part in (raw or "").replace("，", ",").split(","):
        n = part.strip()
        if n and n not in out:
            out.append(n)
    return out


def reload_names() -> None:
    """从配置重算 `SELF_NAMES` / `OTHER_NAMES`（未配置 ⇒ 用占位名）。

    🔑 名单是**部署方的数据**：`.env` 里 `PRIVACY_SELF_NAMES=…` / `PRIVACY_OTHER_NAMES=…`，
    留空就用占位名。这么设计是为了让"谁的数据"不落进被 git 追踪的文件。
    """
    from ..common.config import get_settings

    cfg = get_settings()
    SELF_NAMES[:] = _split_names(cfg.privacy_self_names) or list(DEFAULT_SELF_NAMES)
    OTHER_NAMES[:] = _split_names(cfg.privacy_other_names) or list(DEFAULT_OTHER_NAMES)


reload_names()

# 百家姓（前 ~300，覆盖实际文档里出现的人）
SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳史唐费廉岑"
    "薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄穆萧尹"
    "姚邵汪祁毛狄米贝臧伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强"
    "贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万柯管卢莫房解"
    "应宗丁宣邓郁单杭洪包左石崔吉龚程嵇邢裴陆荣翁荀羊甄曲家封储靳段巫"
    "乌焦牧山谷车侯班秋仲伊宁仇栾甘厉祖武符刘景詹龙叶幸韶郜黎宿白怀蒲鄂"
    "索赖卓蔺屠蒙池乔翟谭劳姬冉雍桑桂牛通边燕浦尚农温庄晏柴瞿阎充慕连"
    "茹艾鱼容向古易慎戈廖虞步耿满匡国文寇广禄东欧蔚越师巩聂晁敖冷辛阚"
)

# 停用词：像名字但不是人名的常见词（按整段匹配）
STOPWORDS = {
    "毕业设计", "毕业", "毕业答辩", "毕业论文", "毕业实践", "毕业总结",
    "个人简历", "个人总结", "个人心得", "个人申请", "个人材料", "个人陈述",
    "工作", "工作总结", "工作计划", "工作汇报", "工作申请", "工作方案",
    "学习", "学习心得", "学习计划", "学习方法", "理论学习", "政治学习",
    "实习", "实习报告", "实习总结", "实习计划", "实习证明", "实习鉴定",
    "实践", "社会实践", "实践报告", "实践总结", "调查报告", "调研报告",
    "申请", "申请书", "申请表", "申请材料", "申辩材料",
    "通知", "通知书", "公告", "名单", "花名册", "汇总", "登记表", "推荐表",
    "计划", "总结", "报告", "方案", "办法", "细则", "指南", "模板", "说明",
    "附件", "材料", "附件一", "附件二", "附件三", "表格", "附件表",
    "思想", "思想汇报", "心得", "体会", "发言", "发言稿", "自荐信",
    "志愿书", "申请书表格", "志愿", "转正", "评审", "培训", "积极分子",
    "信息", "软件", "工程", "学院", "大学", "科技", "公司", "部门", "集团",
    "推荐", "公示", "公示表", "考核", "评优", "评奖", "奖学金", "助学金",
    "证书", "证明书", "毕业证书", "学位", "答辩", "开题", "中期检查",
    "简历", "自传", "政审", "鉴定表", "登记表表格", "承诺书", "保证书",
    # 实测全量 105 份文件名后仅这 3 个「姓+名」候选是误判，先验后加入
    "应用开发", "项目简介", "班徽",
}

# 文件名分段：非中文字符都是分隔符
SEG_SPLIT = re.compile(r"[^一-鿿]+")
NAME_SEG = re.compile(r"^[一-鿿]{2,4}$")

# 英文简历的强特征：独占一行的全大写节标题
CV_SECTION_RE = re.compile(
    r"(?m)^\s*(EDUCATION|EXPERIENCE|WORK EXPERIENCE|PROFESSIONAL SUMMARY|"
    r"SKILLS|PROJECTS|ACHIEVEMENTS|EDUCATIONAL BACKGROUND|PRIZES|AWARDS)\s*$",
    re.IGNORECASE)
# 文件名呈「人名形态」：含两个以上英文单词，或 Resume/CV 之类
CV_NAME_RE = re.compile(
    r"(?i)(resume|\bcv\b|简历)|(?:[A-Z][a-z]{1,15}\s+[A-Z][a-z]{1,15})")

# ---- 名单型文档识别 ----
# 关键教训：不能用「正文出现 ≥5 个像人名的词」来判名单——
# `re.finditer(r"[一-鿿]{2,4}")` 会把任意连续汉字切成滑窗片段
# （"解码器结构"→解码器结/码器结构…），实测把 46/96 份文档误判成名单。
#
# 正确做法：只在**表格行**里找「独立单元格且是 2~4 字人名」。
# 花名册/班级名单的形态就是「一行一个人」：| 郑十 | 20230001000 | … |
# 正文散文里几乎不会出现这种结构，误判率实测 0。
ROSTER_MIN = 5
TABLE_ROW_RE = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$", re.MULTILINE)


def roster_names(text: str, limit: int = 60) -> list[str]:
    """从 markdown 表格行里提取人名单元格，判断是否为名单型文档。

    只认「独立单元格 + 2~3 个汉字 + 首字是百家姓」，避免滑窗误切。
    """
    found: list[str] = []
    seen: set[str] = set()
    for row in TABLE_ROW_RE.findall(text or ""):
        for cell in row.split("|"):
            s = cell.strip()
            # 只认 2~3 字：真实人名几乎都是 2~3 字，4 字多是「章节标题/宋体小四」
            # 这类格式词（实测会把《实习总结报告的书写规范》的字体表误判成名单）
            if not re.fullmatch(r"[一-鿿]{2,3}", s):
                continue
            if s in seen or s in SELF_NAMES or s in OTHER_NAMES or s in STOPWORDS:
                continue
            if s[0] not in SURNAMES:
                continue
            seen.add(s)
            found.append(s)
            if len(found) >= limit:
                break
    return found if len(found) >= ROSTER_MIN else []


def filename_name_candidates(path: str) -> list[str]:
    """从文件名分段里找「姓+名」候选人。整段 2~4 个汉字才算，降低误判。"""
    stem = Path(path).stem
    # 学号/工号等长数字先摘掉，避免 20230001001_王五 里粘连
    stem = re.sub(r"\d{6,}", " ", stem)
    out = []
    for seg in SEG_SPLIT.split(stem):
        seg = seg.strip()
        if not seg or not NAME_SEG.match(seg):
            continue
        if seg in STOPWORDS or seg in SELF_NAMES or seg in OTHER_NAMES:
            continue
        if seg[0] in SURNAMES:
            out.append(seg)
    return out


def _count(text: str, name: str) -> int:
    return text.count(name)


def classify_owner(text: str, filename: str, owner_guess: str | None = None) -> dict:
    """返回 {owner, reason, evidence}。"""
    text = text or ""

    # 0. 本人文档先出局：本人简历也可能是英文排版，不能被「英文简历特征」误判为他人
    for n in SELF_NAMES:
        if n in filename:
            # 文件名点名本人 → 归属明确。但**正文**含本人标识不能短路：
            # 「班级成员基本情况.docx」正文有 29 个人的名单（含本人），
            # 若按正文短路就会判成 self，把整份花名册放进索引。
            # 所以正文里的本人标识只作为弱信号（第 6 步才用）。
            return {"owner": "self", "reason": "文件名含本人标识", "evidence": n}

    # 0.5 名单型文档：正文是多人名册（≥N 个不同姓名），无论本人是否在列都判他人。
    #     实测漏判两份：
    #       学校/0904/班级成员基本情况.docx —— 29 人的学号/寝室/政治面貌
    #       材料/人员发展登记表.xlsx   —— 名单（表格里含他人学号/出生日期/民族）
    #     这类文档即使有本人行，也等于把他人的身份信息写进了检索库。
    roster = roster_names(text)
    if roster:
        return {"owner": "other", "reason": "名单型文档（含多个他人姓名）",
                "evidence": roster[:5]}

    # 1. 英文简历特征：正文有大写节标题（EDUCATION/PROFESSIONAL SUMMARY…）
    #    且文件名是人名形态（英文姓名/拼音）→ 他人简历。
    #    实测漏判：一份全英文简历（形如 `Resume-Jane Doe.docx`，
    #    含电话与邮箱，全程无一个中文字可匹配 —— 中文规则集体失效）。
    sig = CV_SECTION_RE.findall(text)
    if sig and CV_NAME_RE.search(filename):
        return {"owner": "other", "reason": "英文简历特征+姓名形态文件名",
                "evidence": sig[:3]}

    # 2. 文件名点名他人
    for n in OTHER_NAMES:
        if n in filename:
            return {"owner": "other", "reason": "文件名含他人姓名", "evidence": n}

    # 3. registry 登记时已标他人
    if owner_guess == "other":
        return {"owner": "other", "reason": "registry 标记", "evidence": owner_guess}

    # 4. 文件名里的「姓+名」候选（名单外陌生人的泛化防线）
    cands = filename_name_candidates(filename)
    if cands:
        return {"owner": "other", "reason": "文件名含他人姓名", "evidence": cands}

    # 5. 正文出现他人姓名 —— 不设次数阈值（见模块 docstring 的实测理由）
    for n in OTHER_NAMES:
        if n in text:
            return {"owner": "other", "reason": "正文含他人姓名", "evidence": n}

    # 6. 正文含本人标识
    for n in SELF_NAMES:
        if n in text:
            return {"owner": "self", "reason": "正文含本人标识", "evidence": n}

    # 7. 纯英文简历特征（本人已在第 0 步出局，这里剩下的都是他人）
    sig = CV_SECTION_RE.findall(text)
    if sig and CV_NAME_RE.search(filename):
        return {"owner": "other", "reason": "英文简历特征+姓名形态文件名",
                "evidence": sig[:3]}

    return {"owner": "unknown", "reason": "无归属信号", "evidence": None}


__all__ = [
    "OTHER_NAMES",
    "ROSTER_MIN",
    "SELF_NAMES",
    "STOPWORDS",
    "SURNAMES",
    "classify_owner",
    "filename_name_candidates",
    "roster_names",
]