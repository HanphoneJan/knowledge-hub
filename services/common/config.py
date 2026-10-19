"""运行时配置。

所有可调项走环境变量（见 `.env.example`），默认值适配本机开发环境。
配置对象是进程级单例，不要在运行时改。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- Postgres ----------
    pg_user: str = "pkh"
    pg_password: str = "pkh_local_dev"
    pg_db: str = "pkh"
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432

    # ---------- Qdrant ----------
    qdrant_url: str = "http://127.0.0.1:6333"
    #: 🔑 D193：**别名模式**（蓝绿重建的前提）。
    #: 默认 `false` = 逻辑名 `kb_dense` 就是真实集合（与历史行为逐字一致）。
    #: `true` = 逻辑名是**别名** → 物理集合 `kb_dense__<gen>`，于是
    #: `POST /admin/reindex?swap=true` 能"先建影子 → 验证 → 原子切别名 → 留旧回滚"。
    #: ⚠️ 打开前必须先跑一次性迁移 `deploy/bootstrap_index_aliases.py`（把现有真实集合
    #: 复制成物理集合 + 别名）—— 否则 `ensure_collections` 会主动报错。
    qdrant_aliases: bool = False

    # ---------- 模型（D22：全局统一，改动需全量重建索引）----------
    text_embed_model: str = "BAAI/bge-small-zh-v1.5"
    text_embed_dim: int = 512
    vdr_model: str = "vidore/colSmol-500M"
    # 🔑 视觉腿总开关（D124）。**默认开**，关掉是**运维决定**：
    #   colSmol 吃 2.5GB 内存、实测 **68s/图**（与分辨率无关），
    #   而 worker 是单进程串行领任务 —— 导 500 张照片就是 9 个小时。
    #   小团队/笔记本上"我只要文本检索"是合理诉求，所以给一个诚实的开关。
    # ⚠️ 关掉**不是**静默降级：
    #   - 扫描件/照片不再入队视觉任务，文档状态直接 `ready`（不留"处理中"）
    #   - `/admin/consistency` 的 `kb_visual` 腿会照实报"PG 说该有、Qdrant 没有"
    #   - worker 连模型都不加载（`enable_visual(False)`）
    # ⚠️ 改这个开关**不重建**已有索引：已经写进去的页级向量照样能搜，
    #   只是不再新增（photo 库要重新摄取才会补上历史照片）。
    vdr_enabled: bool = True

    # ---------- 视觉查询服务（D158）----------
    #
    # 🔑 **只做编码**（文本 → multi-vectors）。Qdrant 查询与租户过滤
    #   **留在 API 侧** —— `build_qdrant_filter` 是唯一的租户过滤入口之一
    #   （CONVENTIONS §0.2），复制一份到新进程就多一份会漂移的逻辑，
    #   而漂移的方向是"越权看到别人的照片"。
    #: 空串 = **不启用读侧**（视觉腿恒 skipped）。留着它是为了"只跑文本检索"
    # 的部署能显式声明这件事，而不是靠"服务没起"这种偶然状态。
    visual_service_url: str = "http://127.0.0.1:8020"
    #: 🔑 视觉腿是**增强项**：它挂掉只能让那一腿 `skipped`，不能让检索 500。
    #: 实测 6 路并发时单条编码 p95 **905ms**（串行 157ms）⇒ 2s 是**两倍余量**，
    #: 且这个预算**包含排队**（并发上限之内的请求要等）。
    visual_service_timeout_s: float = 2.0
    #: 同时打向视觉服务的请求上限。⚠️ colSmol 在 CPU 上并行度 8.7x
    #: （6 路并发吃 8.7 个核，`CONVENTIONS.md` §4.1c）—— 再往上堆不会更快，
    #: 只会让每条更慢。超限就排队，排队超时就降级。
    visual_service_max_concurrency: int = 4
    #: 探活结果的缓存秒数。⚠️ 不要设成 0：每次检索都探一次活，
    #: 多一次 RTT（而且服务挂掉时那一次 RTT 是**连接超时**，白等）。
    visual_service_probe_ttl_s: float = 5.0

    # ---------- 视觉**写**侧也走服务（D178，SMB 多 worker）----------
    #
    # 🔑 动机：worker 若自己加载 colSmol（2.65GB/进程），多开 worker 就是
    #   多份 2.65GB —— 4 个 worker ≈ +10.6GB。SMB 规模要的就卡在这里。
    #   写侧改成 HTTP 调那个**常驻的**视觉服务，worker 便不再加载 colSmol
    #   （RSS 回落到 ~几百 MB），于是可以安全地多开。
    #: 默认 **关**（保持既有行为：worker 本地编码图片）。SMB 多 worker 部署设 true。
    #: ⚠️ 打开后**摄取依赖视觉服务**：它挂了，照片/扫描件摄取会失败并重试
    #:   （`jobs.is_retryable` 默认可重试）；这是有意的取舍，不是静默降级。
    visual_write_via_service: bool = False
    #: 写侧**单页**图片编码的超时（秒）。⚠️ 与查询的 2s 预算是两回事：
    #:   一页 colSmol 前向实测 ~15s，加上服务端排队要给足余量。
    visual_service_image_timeout_s: float = 120.0

    # ---------- Reranker（阶段 3 · 默认关闭）----------
    # ⚠️ 空串 = 显式禁用（"运维决定"），没有这个字段 = 没装模型（"环境问题"）——
    #   `/search/debug` 要能把两者分开报（见 `retrieval/rerank.py`）。
    # ⚠️ 换模型会**改变排序**（D22 同一条纪律：改动需全量重建并记进 PROGRESS）。
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # ---------- 运行时 ----------
    api_env: str = "local"
    log_level: str = "INFO"
    data_dir: str = "data"

    # ---------- 对外暴露面（D138）----------
    #
    # ⚠️ **默认值是"开着"，这是刻意的**：本机开发、pytest、三个 eval 脚本
    #   与三套 e2e 都要自助注册，默认为关会让整套基线红掉，而"配置默认关"
    #   又意味着"忘了开"很难发现（要等基线红才知道）。
    #   ⇒ 对外部署时在 `.env` 里显式写 `allow_register=false`。

    #: 自助注册（`POST /api/v1/auth/register`）。个人实例**只该有自己**，
    #: 关掉它陌生人就连账号都建不出来（他们只能拿到"邮箱或密码不正确"）。
    #: 🔑 关掉之后要新建账号走 `services/api/scripts/init_dev.py`
    #:    或 `seed_dev.py`（直写库），**不需要**留任何 HTTP 后门。
    allow_register: bool = True

    #: `/docs`（Swagger UI）与 `/openapi.json`：把**完整 API 面**画给任何人，
    #: 包括 MCP 的工具名。调用仍然要 Key，但"摸索"的成本归零了。
    #: ⚠️ 关掉**不影响** `eval/contract_check.py` —— 它读的是 `app.openapi()`
    #:    这个方法，不是那个 HTTP 路由（FastAPI 两者独立）。
    expose_docs: bool = True

    # ---------- 目录导入白名单（D92）----------
    # 🔑 `import/chat` 与 `import/dir` 收的是**服务器上的路径**。
    # 没有白名单的话任何租户都能 `{"path": "/etc"}` 或
    # `{"path": "../../另一个租户的数据"}` 把别人的文件读进自己的库 ——
    # 那不是路径穿越的小瑕疵，是跨租户数据泄漏。
    # 空 = 只允许 `<data_dir>/imports`；多目录用逗号分隔，**按顺序命中**。
    import_roots: str = ""

    # ---------- 对话上下文窗口（D141）----------
    #
    # 🔑 `chat_messages` 一直在写，但生成端**从不回读** —— 每轮问答互相不知道，
    #   而前端每次追问都带 `session_id`（看起来就是多轮对话）。M7 承诺的
    #   "会话历史持久化，**上下文窗口管理**"只兑现了前半句。
    #: 带进生成的最近 N 轮（user+assistant 各算一条 ⇒ N 条消息）。
    #: ⚠️ **0 = 关掉多轮**（回到 D141 之前的行为），不是"无限"。
    ask_history_turns: int = 4

    #: 历史字符预算。按**整轮**丢弃、超了就丢最旧的（`llm.window_history`）。
    ask_history_chars: int = 4000

    #: 追问"那栏填什么"这类**指代式**短问题时，把上一轮问句并进检索 query
    #: —— 与生成器无关，抽取式（默认）也受益。关掉它则多轮只影响生成侧。
    ask_anaphora_expand: bool = True

    #: 指代扩展的问题长度上限：**超过这个长度**说明用户把话说全了，不该加料。
    ask_anaphora_max_chars: int = 24

    # ---------- 生成层（D2：统一走 HTTP，模型可插拔）----------
    #
    # ⚠️ 默认 `extractive` 是**隐私红线的要求**，不是省事：
    # 检索出的内容是聊天记录 / 简历 / 他人文档片段，发给外部 API 等于把
    # 私人数据送出本机。所以 `llm_provider=openai_compat` 必须同时配齐
    # base_url / model / api_key 才生效，否则 `get_generator` 直接抛错
    # （**不静默降级**成抽取式 —— 那会让用户以为自己在用 LLM）。
    llm_provider: str = "extractive"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

    # ---------- 连接池 ----------
    # 12 核机器上 API 进程以 IO 为主，池不宜大；worker 另设。
    pg_pool_size: int = 5
    pg_max_overflow: int = 5

    # ---------- trgm 腿的规模闸（D180）----------
    #
    # 🔑 trgm 腿的召回条件是 `word_similarity()`，**只能 seq scan**（D70），
    #   实测 ~55 字/行的合成语料：1 万 ≈ 0.18s · 10 万 ≈ 1.4s · 100 万 ≈ 13.9s。
    #   D46 要求检索段 < 500ms ⇒ 库一大就必须给这一腿封顶，否则每次检索都被它拖死。
    # ---------- 归属判定（隐私隔离）----------
    #: 「本人」标识（逗号分隔）。**部署方的数据，不是程序常量** ⇒ 仓库里留空、
    #: 真实值写进 `.env`（不进 git）。留空时用 `ingest/privacy.py` 的占位名。
    privacy_self_names: str = ""
    #: 「他人」显式名单（逗号分隔）。同上：留空用占位名。
    privacy_other_names: str = ""

    # ---------- 开发 / 演示账号（脚本用，**不要**在公开仓库里写死真实账号）----------
    #: 本地开发种子账号的口令（`services/api/scripts/{init_dev,seed_dev}.py`）。
    #: 留空 ⇒ 那两个脚本拒绝运行。**不要**把真实口令写进仓库。
    seed_dev_password: str = ""

    #: `deploy/seed_photos.py` 建的持久照片库账号。
    #: ⚠️ 这两个是**演示/开发**账号；生产用自己的 `.env`，别沿用示例值。
    keeper_email: str = "photos@keeper.local"
    keeper_password: str = ""

    #: 语句超时（毫秒）。超时就**降级成 skipped**（如实说明，不是静默丢）。
    #: `0` = 不限（库很小时没必要封顶；也方便想量全量耗时的场景）。
    trgm_statement_timeout_ms: int = 800

    @property
    def pg_dsn(self) -> str:
        """同步 DSN（worker / alembic 用）。"""
        return (
            f"postgresql+psycopg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )

    @property
    def pg_async_dsn(self) -> str:
        """异步 DSN（API 用）。"""
        return (
            f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )

    @property
    def data_path(self) -> Path:
        return ROOT / self.data_dir

    @property
    def blob_path(self) -> Path:
        """内容寻址存储根目录（D35 双份存储的"原文"侧）。"""
        return self.data_path / "blobs"

    @property
    def thumb_path(self) -> Path:
        return self.data_path / "thumbs"

    @property
    def is_local(self) -> bool:
        return self.api_env == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
