# 文档摄取：从文件到可检索

一次上传要经过解析、切分、校验、掩码、嵌入和派生任务。链路的形状如下：

![摄取链路：上传校验、blob 落盘、documents 行、worker 解析切分校验、写文本索引，再派生视觉与预览任务](diagrams/ingest-pipeline.svg)

入口在 `services/api/routers/documents.py`，主流程在 `services/ingest/pipeline.py` 的 `ingest_document()`，由 worker 的 `handle_ingest` 调用。三个顺序约束贯穿全链路：

- **切分策略在解析之后、掩码之前。** 策略作用于内容（语义分组、父块、上下文生成），掩码必须作用在最终要入库的文本上。
- **PII 先检出再掩码。** 计数必须记掩码前的，否则开了掩码的库报告永远显示 0 处。
- **扩展名在数据库行里，不在 blob 路径里。** 内容寻址存储的路径是 `{sha}/original`，没有扩展名；解析器按 `documents.file_name` 分派。看 `path.suffix` 会让所有文件都报“不支持的格式”。

## 上传时先挡掉哪些文件？

能在请求里判定的错误不拖到 worker：

| 检查 | 结果 |
|---|---|
| 扩展名不在白名单 | `415 UNSUPPORTED_FORMAT` |
| 单文件超过 50 MB | `413 FILE_TOO_LARGE` |
| 单请求超过 20 个文件 | `413` |
| 空文件 | `422` |
| 文件头与扩展名明显不符 | `415`，但只在**确凿**时拒绝 |

文件头嗅探是保守的：认出 `%PDF`、`PK`、PNG/JPEG/WebP/BMP/TIFF 魔数才判断；识别不出、或期望文本却看不出二进制，一律放行。把“奇怪但合法”的文件挡在门外，比放进一份坏数据更糟。

通过后按 sha256 落盘，登记 `blobs` 行（引用计数从 1 开始），再写 `documents(status=pending)`。同名旧版本在这里被 `supersede` 进回收站，新版本 `version_id` 加一。

## 解析器派发表如何决定格式？

`services/ingest/parsers.py` 里有一张 `PARSERS`，后缀映射到处理器。加一种格式只改这张表：

| 后缀 | 处理方式 | 有坐标 |
|---|---|---|
| `.pdf` | pymupdf 逐页解析，表格与段落分开 | 是 |
| `.docx` | python-docx，按正文 XML 顺序遍历，`Heading N` 样式还原成 `#`×N | 否 |
| `.xlsx` | openpyxl，按工作表解析，首行当表头，列按下标对齐，块文本带 `[工作表: <name>]` 前缀 | 否 |
| `.pptx` | python-pptx，每张幻灯片一个 chunk | 否 |
| `.md` `.markdown` `.txt` `.json` | 读文本，按 Markdown 标题与段落切 | 否 |
| `.html` `.htm` | 标准库 `html.parser` 抽 `h1~h6` 与正文，再按 Markdown 切 | 否 |
| `.csv` | 首行当表头，按行分组 | 否 |
| `.jpg` `.jpeg` `.png` `.webp` `.bmp` `.tiff` | 没有文本层，标记为扫描件 | 否，只走视觉腿 |

上传白名单是派生的：

```python
SUPPORTED_SUFFIXES, IMAGE_SUFFIXES = _derive_suffix_sets(PARSERS)
```

这样测试可以断言“白名单确实等于派发表推出来的结果”，有人再手写一份清单就会被抓住。派发记录统一返回 `(chunks, scan_pages, is_scanned)`；只返回块列表的文档解析器由适配器补成三元组，避免每个调用点各自记着补 `[], False`。

## 六种切分策略怎么选？

切分是知识库级单选（`knowledge_bases.chunk_strategy`），默认 `recursive`。没有通吃的一种：

| 策略 | 边界 | 前提或代价 | PDF |
|---|---|---|---|
| `fixed` | 定长 + overlap 硬切 | 会把句子切断；存在的意义是评测基线 | 不执行 |
| `recursive`（默认） | 标题 > 段落 > 硬切 | 不传策略与显式传 `recursive` 输出逐字相同 | 执行 |
| `semantic` | 相邻句向量相似度低于 0.55 就断开 | 需要句向量编码器；不做 overlap | 不执行 |
| `parent_child` | 子块是递归块，父块是章节（文本）或整页（PDF） | 父块不进向量库，只随 payload 回传 | 按页分组 |
| `late` | 边界同 `recursive`，但整篇过一次 Transformer 再按 span 池化 | 受 bge-small 512 token 上限约束，超长会截断 | 执行 |
| `contextual` | 边界同 `recursive`，每块前置一句 LLM 生成的上下文 | 需要显式配置外部 LLM；没配就退回默认并记 note | 执行 |

`fixed` 与 `semantic` 之所以对 PDF 不执行，是因为它们会重排文字。坐标依赖“块文本与页面位置一一对应”，重排后坐标全部失效。`semantic` 的阈值是 0.55，且刻意不叠 overlap：语义边界本身已经是一个完整句子组。

`STRATEGIES` 是“策略名 → 实现标签”的注册表，真正分派在 `apply_strategy()`。API 白名单与知识库字段校验都从它派生，测试会断言注册表与对外 schema 里的枚举相等，避免“白名单里有、实现没有”的漂移。

策略失败一律降级，不会让摄取失败。未知策略名、策略内部抛错、依赖缺失（没有编码器或没有 LLM）都退回 `recursive`，原因写进 `documents.quality.strategy_notes`。用户能看见“我选的策略没生效”。

策略是单选 profile，Parent-Child 与 Late 理论上能叠加，但组合爆炸，本期不做。**改策略只对之后重新摄取的文档生效**；`GET /kb/{id}` 的 `stale_strategy_docs` 就是“还停在旧策略的当前文档数”，界面据此提示重摄取欠账。

## 校验分几层，失败如何处置？

`services/ingest/validate.py` 是纯函数，不碰数据库、不加载模型，所以每条判据都能直接单测。

| 层 | 检查 | 失败处置 |
|---|---|---|
| 格式 | 扩展名白名单 + 文件头嗅探 | 明显不符 → 上传 415 |
| 解析 | 空内容、乱码率 > 5%、长度 < 100 字或 > 1000 万字 | 空内容 → `failed`；乱码与长度 → 记 `quality`，不阻断 |
| 切分 | 块大小分布：最小 < 50 字、最大 > 5000 字、标准差 > 均值一半 | 写 `quality` 与日志，不阻断 |

解析层有一条关键分支：解析出 0 个块且**不是扫描件**时，抛 `PARSE_EMPTY`，文档置 `failed`。在这之前，空的 Office 文档会走“当成扫描件”的分支直接 `ready`，静默入库一个空文档。扫描件是另一回事：它本来就没有文本层，`parse` 段记 `{"skipped": "扫描件/照片，无文本内容，走视觉腿"}`，不是“通过”。

`documents.quality` 里能读到完整结论：

```jsonc
{
  "parse":    {"ok": true, "fatal": false, "garbled_rate": 0.0, "char_count": 18432, "issues": []},
  "chunking": {"total": 74, "avg_size": 249.1, "min_size": 62, "max_size": 511,
               "stddev": 88.3, "acceptable": true, "errors": [], "warnings": []},
  "strategy": "recursive",
  "encoding": "standard",
  "strategy_notes": []
}
```

乱码率判据是 5%：4.9% 放行、5.1% 记问题，这两个边界就是普通单测用例。`status_msg` 只放给用户看的一句话，完整原因在 `quality` 与 `/jobs` 里。

## 坐标从哪里来？

坐标（L3）只对 PDF 原生可得，其余格式要么没有，要么从转换版补。PDF 的解析顺序是：

1. 每页先找表格，表格内文字不再进段落；
2. 收集整页的行，按阅读顺序排序；
3. 按缩进与句末标点切段落，每段一个 bbox；
4. 归一化 bbox 到 0~1。

判定扫描页不能只看“有没有文本”：

```python
if not page.get_text().strip():
    if page.get_images(): scan_pages.append(pno)   # 扫描页，走视觉腿
    else:                 blank_pages.append(pno)  # 空白页，跳过
```

把空白页算成扫描页会白烧 15 秒编码；矢量图形没有文本层但不是扫描件，也一样。整份文档一页文本层都没有才算 `is_scanned`。普通 PDF 上限 2000 页；扫描件另有 50 页上限，超过就拒绝并说明要花多少分钟。

多栏 PDF 用轻量 XY-cut：找一条没有任何行水平跨越的竖直切线，递归地先左后右，横跨切线的通栏标题放最后。单栏文档退化成“先 y 再 x”，与旧行为逐字相同。

Office 没有原生坐标。`pdf_preview` 任务把 Office 转成 PDF 后，在**转换版**上重新定位每个 chunk，实测平均覆盖约 99%。定位不到的 chunk 故意不给框。由此得到一条不变量：**坐标与被渲染的页图必须来自同一个文件**。PDF 用原件，Office 用转换版；带框预览的 `src` 由调用方传进来，渲染函数不许自己去找原件，否则给 docx 画框会直接报错。

判断“有没有坐标”要看内容，不能看 `locations IS NULL`。这一列是 JSONB，空列表常常被存成 JSON `null` 而不是 SQL `NULL`，用 SQL 判会一条都筛不出来。`services/ingest/locations.py::has_locations` 是唯一判据。

PDF 的页眉页脚在解析期单独清：`locations._repeated_lines` 先扫全文，找出在至少 3 页出现、且短于 40 字的精确重复行，在**分段之前**丢掉。因此 bbox 只覆盖保留下来的正文行，L3 不变量不受影响。带页码的页眉（“第 3 页”与“第 4 页”文本不同）不会被删——宁可留噪声，也不误删正文。文本格式的清洗（`chunker.clean_text`）用同一套判据，另外去零宽字符、把连续空行压到两行。

## 表格怎么结构化？

一份表格网格会投影成三份产物：

| 产物 | 落点 | 给谁 |
|---|---|---|
| Markdown 表 | `chunks.content_norm` 后半段 | 人读与生成上下文 |
| 一句规则描述（“N 行 × M 列；表头：…”） | `content_norm` 开头 | 检索，让“一览表”类查询能命中 |
| 结构化网格 `{header, rows, row_count, col_count}` | `chunks.table_json` | 前端渲染真表格、程序化读取 |

三份来自同一次 `tbl.extract()`，不各自解析。描述是纯规则生成，不外传；需要 LLM 描述必须显式配置外部 LLM。

坐标不能从扁平的 `tbl.cells` 取：

```python
len(tbl.cells) != row_count * col_count   # 合并单元格会让长度对不上
tbl.rows[ri].cells[ci]                    # 与 extract() 的行一一对应，None = 合并单元格
```

文本用 `extract()`，坐标用 `rows[i].cells`，按行号对齐。表格超过 200 个框时按整行拆成多个 chunk，每组重复表头，而不是只留一个外框——只留外框等于退化成页级高亮。

XLSX 与 CSV 还有一条：表头与数值必须按列下标对齐。各自过滤空值会让中间缺一格时整列错位。

表格结构化目前只覆盖 PDF、CSV、XLSX。DOCX、HTML、PPTX 里的表格仍按纯文本处理，这是已知边界。

## PII 与归属判定在哪一步？

PII 检出在切分之后、写 PG 之前。检出类型包括身份证、手机号、邮箱、银行卡、学号。三条约束：

1. **默认关闭，按库开关。** 个人知识库的常见用途就是搜自己的号码，默认掩码等于把这个用途关掉。
2. **只掩索引侧。** blob 原件、预览页图、下载文件都保持原文；代价是开了掩码就搜不到自己的号码。
3. **报告只读。** 只回答哪里有、有多少，不返回原文片段，否则报告本身成了新的泄露面。

掩码会改变文本长度，影响很小，因为高亮用的是块级 bbox 而不是字符偏移。实现里有两个不报错但结果错的细节：同跨度的重叠命中要去重（18 位数字可能同时命中身份证与银行卡）；掩码长度要自适应（固定写法对 6~7 位数字会比原文更长）。

归属判定（`services/ingest/privacy.py`）用于导入历史语料时判断“这份文档是不是本人的”。它不靠硬编码名单，而是规则式：从文件名分段里识别“姓 + 1~3 字名”的候选，配停用词表排除“毕业设计”这类误判。优先级从高到低是：文件名含本人 → 名单型文档 → 英文简历特征 → 文件名含他人 → 注册表标他人 → 文件名含姓名候选 → 正文含他人姓名 → 正文含本人 → `unknown`。

“正文含他人姓名”这一条不设次数阈值，是刻意的：设阈值会漏判正文里只出现两次的他人自传，以及表格里列着他人学号与出生日期的花名册。误判方向（把本人文档判成他人，少索引几条）可接受，漏判方向不可接受。

聊天切片不能拿这个判定当门禁。实测每个 50 条切片几乎都判 `other`，因为聊天必然出现对话方名字；拿它当门禁会把整个聊天库导空。所以导入任务只记录判定，不拦截。

## 三种导入入口有什么不同？

除了浏览器上传，还有两个服务端导入入口，二者都只入队、不同步跑：

| 入口 | 目标库类型 | 输入 | 产出 |
|---|---|---|---|
| `POST /kb/import/chat` | `source_kind=chat` | 服务器上的 `.jsonl` | 每 50 条消息切一个 Markdown 文档，`doc_source=chat_import` |
| `POST /kb/{id}/import/dir` | `source_kind=photo` | 白名单目录里的图片 | 每张图一个文档，`doc_source=photo_dir` |

聊天切片不按日期切，而按固定 50 条切：按日期会产出“一天 3000 条”的超大文档和“一天 1 条”的碎片，实测文档数会从 666 变成 1740。切片文件名带序号与日期范围，时间戳与说话人都进正文，uid 不进正文——uid 是对话方的账号标识，属于 PII。重复导入按“文件名 + 内容哈希”幂等，内容变了的同名文件走覆盖式更新。

导入端点收的是服务器路径，所以有 `IMPORT_ROOTS` 白名单，默认只允许 `<data_dir>/imports`。路径先 `resolve()` 再判前缀，并用 `is_relative_to` 而不是字符串前缀，否则 `data/imports_evil` 会被误判在 `data/imports` 里。

## 失败与重试怎么处理？

`IngestError` 带一个 `retryable` 标志。blob 缺失、格式不支持、超过页数上限这类错误是永久的，直接置终态 `failed`，不再占用重试次数；网络抖动、视觉服务暂时不可用这类瞬时错误回到 `pending`，最多重试 3 次。默认方向是“宁可多试两次”，只有明确声明 `retryable=False` 的才不重试。

失败后用户有两个入口：`POST /documents/{id}/retry` 只接受 `status=failed` 的文档；`POST /documents/{id}/reparse` 不要求失败，改了切分参数后用它重新摄取。两者都会重新入队 `KIND_INGEST`。

## 入库之后怎么判断没漏？

| 症状 | 先查什么 |
|---|---|
| 文档 `ready` 但搜不到 | 三个集合里这个 `doc_id` 有没有点；文本腿能靠重建修，视觉腿要重新摄取 |
| 状态卡在中间态 | 是否还有未完成任务；“没有待办任务却还在中间态”是不可能的状态，worker 启动会补扫 |
| 某一类内容完全搜不到 | 该腿能力是否可用（`GET /health` 的 `available_legs`），而不是“库里没有这种数据” |
| 页数对不上 | 视觉腿一页一个任务，同一文档同时有多条未完成视觉任务是正常的；去重键必须带 payload |
| 检索质量悄悄变差 | BM25 词表统计量；它在入库时加、删除时减，绕开统一删除入口就会虚高，idf 被压平 |

最后一条没有报错，只会让罕见词不再罕见。对账里专门有一条判据：`chunks` 行数应等于 `corpus_stats.n_docs`。细节见[数据模型与一致性](data-model.md)。

## 相关文档

- 切出来的块如何被检索：[检索与问答](retrieval.md)
- 表结构与删除链：[数据模型与一致性](data-model.md)
- 摄取任务如何排队与结算：[任务、进程与运维](jobs-operations.md)
