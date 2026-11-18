# Knowledge Hub · 文档在哪

项目文档已随个人技术站一起维护，本仓库不再保留副本：`docs/` 现在只有 API 契约（`DESIGN-API.md`）与界面截图（`img/`）。下面每一行的左侧是原文档标题，右侧是 [docs.hanphone.cn](https://docs.hanphone.cn/docs/项目/KnowledgeHub.html) 上的对应页面 —— 正文、配图与后续更新都以站点为准。

| 原文档标题 | 站点页面 | 说明 |
|---|---|---|
| 架构与进程 | [架构总览](https://docs.hanphone.cn/docs/项目/KnowledgeHub/架构总览.html) | 三个进程与端口、模块依赖、PG/Qdrant/blob 的数据平面 |
| 文档摄取 | [文档摄取](https://docs.hanphone.cn/docs/项目/KnowledgeHub/文档摄取.html) | 上传到 ready 的每一步、格式派发、六种切分、失败分类 |
| 检索 | [检索与问答](https://docs.hanphone.cn/docs/项目/KnowledgeHub/检索与问答.html) | 四腿召回、RRF、查询路由、精排与降级 |
| 问答 | [检索与问答](https://docs.hanphone.cn/docs/项目/KnowledgeHub/检索与问答.html) | SSE 帧序、证据组装、抽取式/外部 LLM、引用与多轮 |
| 数据与一致性 | [数据与一致性](https://docs.hanphone.cn/docs/项目/KnowledgeHub/数据与一致性.html) | 18 张表、内容寻址、软删与版本、删除链、对账与重建 |
| 多租户与权限 | [多租户与权限](https://docs.hanphone.cn/docs/项目/KnowledgeHub/多租户与权限.html) | API Key 推导上下文、三档可见性、两个过滤入口 |
| 任务与运维 | [运维与验收](https://docs.hanphone.cn/docs/项目/KnowledgeHub/运维与验收.html) | 八种任务、文档终态、巡检、备份恢复、蓝绿重建 |
| 对外接口 | [对外接口](https://docs.hanphone.cn/docs/项目/KnowledgeHub/对外接口.html) | HTTP/MCP/A2A 如何共用检索与问答内核 |
| 测试与验收 | [运维与验收](https://docs.hanphone.cn/docs/项目/KnowledgeHub/运维与验收.html) | 测试分层与 CI 门禁在这一篇的「六层测试」一节 |
| 失败模式 | [运维与验收](https://docs.hanphone.cn/docs/项目/KnowledgeHub/运维与验收.html) | 排障顺序与静默失败清单在这一篇的收尾几节 |

`docs/` 里另外两份不随站点迁移：`DESIGN-API.md` 是 82 个端点的契约（`eval/contract_check.py` 会读它，属于代码的一部分），`img/` 是根 `README.md` 内嵌的界面截图。

站点上还有三篇本仓库从未有过的页面，读正文之前先用它们建立背景：

| 页面 | 什么时候读 |
|---|---|
| [导读](https://docs.hanphone.cn/docs/项目/KnowledgeHub.html) | 全书目录、三条主链路与常用命令 |
| [小白概念指南](https://docs.hanphone.cn/docs/项目/KnowledgeHub/小白概念指南.html) | BM25、ANN、RRF、late interaction 的通用原理 |
| [面试准备指南](https://docs.hanphone.cn/docs/项目/KnowledgeHub/面试准备指南.html) | 自我介绍话术与高频追问索引 |
