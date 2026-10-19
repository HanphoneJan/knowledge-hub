"""认证与租户过滤 —— fail-closed 的单点。

**这是整个系统里最需要小心的模块。** `DESIGN-MODULES.md` 第 2 节把它列为
硬性约束：`build_filter` 是**唯一**构造 Qdrant 过滤条件的地方。任何新增接口
都必须调它，不允许自己拼 filter —— 否则漏一处就是越权。

设计要点（借鉴 RAGFlow 的做法：请求体里不放租户字段，服务端从登录态推导）：
1. 请求结构体**不接受** `user_id` / `workspace_id` / `kb_id` 之外的租户参数
2. 所有租户范围由 `UserContext` 推导，它只从 API Key 查出来，不可由调用方指定
3. 无权限与不存在返回同一个错误码，阻断存在性探测

## 三个过滤构造器，同一个真相源

| 函数 | 服务对象 | 用在哪 |
|---|---|---|
| `build_qdrant_filter` | Qdrant | dense / sparse / visual 三腿 |
| `build_kb_scope_sql` | Postgres | trgm 子串腿（`retrieval/trgm.py`） |
| `visible_kb_clause` | Postgres | KB **列表**（`routers/kb.py` 的 `GET /kb`）|
| `writable_kb_clause` | Postgres | KB **回收站**（与 `can_write_kb` 同语义，D129）|

前两者都只读 `UserContext.readable_kb_ids`，**不各自重算三档可见性** ——
本项目唯一一次真实越权（CONVENTIONS §0.6）就是"两处复刻可见性逻辑"造成的。
新加任何检索存储时，往这张表里加一行，不要在调用方拼条件。

🔑 **`visible_kb_clause` 是第三行，也是 D128/D129 的产物**：三档可见性本来在
`load_user_context` 与 `routers/kb.py::_visible_kb_filter` 各写了一份 ——
**两份**就已经是那个越权的成因（D125 的教训：承诺有、代码没有；D129 的教训：
同一段判定写两遍就会开始漂）。加"回收站列表"时如果再写第三份，
就是给同一条历史漏洞再开一个口子，所以先把两份**收成一份**。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, exists, false, literal, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import Forbidden, InvalidToken, ResourceNotFound

# 明文 key 前缀，便于用户识别（key_prefix 单独存库里做 UI 展示）
KEY_PREFIX = "sk-pkh"
KEY_PREFIX_LEN = len(KEY_PREFIX)

#: `api_keys.key_hash` 的长度（**完整** sha256 的 hex 字符数）。
#:
#: 🔴 D134：从 12 改成 64。这不是"格式美化"，是一道**在线爆破**的防线 ——
#: 旧实现只存 sha256 的**前 12 个 hex**（48 bit）。攻击者随便生成 Key、
#: 比对存下来的这 48 bit，平均 **2^48 次哈希** ≈ 一块 GPU **8 小时**。
#:
#: ⚠️ 旧 docstring 的理由是"key 本身是 256 位随机数，没有低熵可爆破"——
#: 那句话只对**离线**成立（拿到库里那 48 bit 也猜不出原文）。
#: **在线**要猜的是"哈希前缀"，不是原始 Key 的全部位 ⇒ 成本降到 2^48。
#:
#: ✅ 代价：改之前发出的 Key **全部失效**（库里存的是 48 bit，验不出来）。
#: 表现是"被登出一次"—— 前端 `lib/auth.ts` 本来就会在 401 时重新登录换 Key。
KEY_HASH_LEN = 64


def generate_api_key() -> tuple[str, str, str]:
    """生成新 API Key。

    返回 `(明文, sha256哈希, 展示前缀)`。
    明文只在创建时返回一次，库里只存哈希 —— 与 D3 一致。
    """
    raw = secrets.token_urlsafe(32)
    plaintext = f"{KEY_PREFIX}_{raw}"
    return plaintext, hash_key(plaintext), plaintext[: KEY_PREFIX_LEN + 6]


def hash_key(plaintext: str) -> str:
    """API Key → 存储用哈希（**完整** sha256，D134）。

    用 SHA-256 而非 bcrypt/argon2：key 本身是 256 位随机数，没有低熵可爆破，
    且需要在每次请求上算（要快）。用户密码才需要慢哈希（用 argon2，见 auth 路由）。

    🔑 **必须存完整长度**（`KEY_HASH_LEN = 64`），见上面那条常量的推导：
    截断过的哈希能被**在线**爆破 —— 猜的是"前缀"，不是原文的全部位。
    ⚠️ 不要为了"省 52 个字符"再截断：那是把一道 2^256 的防线换成 2^48。
    """
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    assert len(digest) == KEY_HASH_LEN, "sha256 的 hex 长度变了？常量要跟着改"
    return digest


def key_prefix_of(plaintext: str) -> str:
    return plaintext[: KEY_PREFIX_LEN + 6]


# ============================================================
# 登录上下文
# ============================================================


@dataclass(frozen=True)
class UserContext:
    """一次请求的授权上下文。

    字段全部是**推导结果**，不是客户端输入。任何路由要判断"能不能访问某资源"，
    都用这里的集合，不要重新查。
    """

    user_id: int
    display_name: str
    email: str
    api_key_id: int | None
    scopes: frozenset[str] = field(default_factory=frozenset)

    #: 我加入的工作区
    workspace_ids: frozenset[int] = field(default_factory=frozenset)
    #: 我可读的 KB 全集（private 归我所有 + workspace 成员可见 + global 全可见）
    readable_kb_ids: frozenset[int] = field(default_factory=frozenset)
    #: 🔑 D173：全局超级管理员。为真时**越过**三档可见性（读写所有库、管所有工作区）。
    #: ⚠️ 它仍然是"从 API Key 推导"的（见 `load_user_context`），请求体里
    #: 不接受这个字段 —— 与 D8/§0.5 一致。
    is_superadmin: bool = False
    #: 昵称（可作登录名，D174）。仅用于展示/审计，不参与授权判定。
    nickname: str | None = None

    # ---- 授权判定 ----

    def has_scope(self, scope: str) -> bool:
        """D4：暂不做细粒度 RBAC，scope 只做粗粒度门禁。

        当前所有 key 都有 `kb:read` / `chat:read`；写操作靠资源归属判定，
        不靠 scope（scope 是给未来 RBAC 留的位置）。
        """
        return scope in self.scopes

    def can_read_kb(self, kb_id: int) -> bool:
        """能否读取某个 KB。三档可见性在推导阶段已折叠成这一个集合。"""
        return kb_id in self.readable_kb_ids


def _workspace_alive(model, workspace_model=None):  # noqa: ANN001
    """EXISTS(工作区没被软删)。

    🔴 **D130：`DELETE /workspaces/{id}` 之前是"半删"的** ——
    `delete_workspace` 只标记 `workspaces.deleted_at`（D42 明确**不级联**
    其下的 KB/文档，为了让恢复只有一行）。实测后果：

    ```
    删除工作区之后：
      GET /kb          → 仍然列出它的库
      GET /kb/{id}     → 仍然 200
      POST /search     → 仍然搜得到正文（实测命中 1 条，text 是原文）
      GET /kb/trash    → 空的（谁也恢复不了，也看不见）
    ```

    也就是说"删掉工作区"对**保密**毫无作用：用户以为内容已经撤走了，
    而它照样能被搜到（而且 30 天后 `purge_containers` 会突然把库删掉）。

    ✅ 修法是让它**真的生效**，而不是改成级联（级联会把 D42 的"恢复一行"
    变成"恢复一串"，而且它当初不级联是有意的）。做法：**在可见性判定里
    要求它的工作区还活着** —— 一处条件，四条路径同时被覆盖：

    | 路径 | 经过什么 |
    |---|---|
    | `GET /kb` | `visible_kb_clause` |
    | 检索/问答（四条腿） | `readable_kb_ids` ← `visible_kb_clause` |
    | `GET /kb/{id}`、写断言 | `can_read_kb` ← `readable_kb_ids` |
    | `GET /kb/trash` | `writable_kb_clause` |

    ⚠️ 顺带把"先恢复工作区"这条**顺序**变成真的：工作区还软删着的时候，
    它的库既搜不到、也恢复不了 —— 与 `GET /kb/trash` 的文档说明一致。
    """
    if workspace_model is None:
        from .models import Workspace as workspace_model

    # 🔑 **必须显式 correlate**：直接 `exists().where(ws.id == model.workspace_id, …)`
    #   会被渲染成 `EXISTS (SELECT * FROM workspaces, knowledge_bases WHERE …)`
    #   —— 子查询里**自己**带了一张 `knowledge_bases`，于是它与外层那一行
    #   **没有关联**，只要"库里存在任意一行满足条件"就为真。
    #   实测：软删了工作区 1 之后，含 `workspace_id=1` 的库**仍然**被判可见
    #   （症状是"判据看起来在工作、实际什么都没挡住"）。
    #   `.correlate(model)` 把它钉回外层那一行。
    return exists(
        select(literal(1))
        .select_from(workspace_model)
        .where(
            workspace_model.id == model.workspace_id,
            workspace_model.deleted_at.is_(None),
        )
        .correlate(model)
    )


def _resolve_kb_model(model):
    if model is None:
        from .models import KnowledgeBase as model

    return model


def _with_workspace(clause, model, workspace_id):  # noqa: ANN001
    return clause if workspace_id is None else and_(clause, model.workspace_id == workspace_id)


def _visible_kb_clause(user_id: int, workspace_ids, *, model=None,  # noqa: ANN001
                       workspace_model=None, workspace_id: int | None = None):
    """"我能**读**的 KB"（PG 侧）。零件版，见 `visible_kb_clause` 的说明。

    🔑 为什么收一个"零件版"：调用方常常在 `UserContext` 还没建出来的时候
    就要用它 —— `load_user_context` 就是（可读范围是 ctx 的**一个字段**）。
    收 ctx 的版本第一版就写成了"先查范围再建 ctx"，而 `UserContext` 是
    **frozen** dataclass：改字段会 `FrozenInstanceError`，不先建又会
    `UnboundLocalError` —— 两个都是**运行时 500**，单测测不到（§5.122）。
    收零件就没有这个顺序陷阱。
    """
    model = _resolve_kb_model(model)
    conds = [
        # global：所有用户可见（D43）
        model.visibility == "global",
        # private：只有创建者
        model.created_by == user_id,
    ]
    if workspace_ids:
        conds.append(
            (model.visibility == "workspace")
            & model.workspace_id.in_(workspace_ids)
        )
    else:
        # 我不属于任何工作区 → workspace 库一概不可见。
        # ⚠️ 这一支**不能省**：`.in_(空集合)` 在部分方言下可能被优化掉，
        # 而"条件自己消失"= 变成"全都可见"。
        conds.append(false())
    # 🔴 D130：工作区自己被软删 → 它的库也不可见（"删掉工作区"要真的生效）
    #
    # ⚠️⚠️ 这一条必须用 **AND** 包住整个 or_，**不能**当成第四条 or 分支 ——
    #   写成 `or_(三档, EXISTS)` 时，只要库里**存在任意一行**满足 EXISTS，
    #   每一行就都通过了：三档里 workspace+成员那一支照样命中
    #   （实测：软删了工作区，它的库仍然"可恢复"）。
    #   ⚠️ 这与 §0.6 那个真实越权是**同一个形状**：
    #   "把限定条件追加成另一条 or" = 限定条件完全无效。
    return _with_workspace(
        and_(or_(*conds), _workspace_alive(model, workspace_model)),
        model,
        workspace_id,
    )


def visible_kb_clause(ctx: UserContext, *, model=None, workspace_model=None,
                      workspace_id: int | None = None):
    """"我能**读**的 KB"这个条件（**PG 侧**，列表用）。

    🔑 **读侧可见性的唯一实现**（CONVENTIONS §0.6）。历史上它有两份 ——
    `load_user_context` 与 `routers/kb.py::_visible_kb_filter` ——
    而"两处复刻可见性"正是本项目唯一一次真实越权的成因。
    现在连"回收站列表"（D129）也走另一个**写侧**实现 `writable_kb_clause`：
    **三份就一定会开始漂**。

    ```python
    global     → 所有用户（D43）
    workspace  → visibility == 'workspace' AND workspace_id IN (我的工作区)
    private    → created_by == 我
    ```

    ⚠️⚠️ 三个分支必须**分别**判定，写在一起会越权。实测踩过的两个错：

    1. `visibility.in_(("workspace","global"))` —— **错**。
       `workspace` 可见性的 KB 只有**该工作区的成员**能看，
       对所有人可见的是 `global`。写成 `in` 等于把全系统所有
       workspace 库泄露给任何用户（实测 Bob 不在 wsA 也能看到 wsA 的库）。

    2. private 加上"同工作区即可见" —— **错**。
       private 的定义就是"只有创建者"。同工作区成员可见的是 `workspace`。

    ⚠️ 而 `workspace` 那条的两个条件必须写在**同一个** `and_` 里：
    拆成两条 `or` 分支的话，限定条件**完全无效**，
    等于"只要可见性是 workspace 就可见"（§0.6 里那个二次错误）。

    ⚠️ 与**写侧**（`writable_kb_clause`）差一格，别混用：见那张表。

    @param model 换一张表（默认 `KnowledgeBase`）。给模型做参数而不是
        在函数里 import，是为了避免 `auth → models` 的导入环。
    @param workspace_id 额外限定某个工作区（列表页的筛选）。
    """
    if ctx.is_superadmin:
        # 🔑 D173：超管越过三档可见性 —— 所有库都可见（含 private 的）。
        # ⚠️ 仍保留 workspace_id 筛选（列表页"按库筛"）。
        return _with_workspace(true(), _resolve_kb_model(model), workspace_id)
    return _visible_kb_clause(
        ctx.user_id, ctx.workspace_ids, model=model, workspace_model=workspace_model,
        workspace_id=workspace_id,
    )


def _writable_kb_clause(user_id: int, workspace_ids, *, model=None,  # noqa: ANN001
                        workspace_model=None, workspace_id: int | None = None):
    """"我能**改**的 KB"（PG 侧）。零件版，见 `_visible_kb_clause` 的说明。"""
    model = _resolve_kb_model(model)
    conds = [
        # global：任何用户可改（D43，与读一致）
        model.visibility == "global",
        # workspace：**必须**「可见性是 workspace」且「我是成员」同时成立。
        # ⚠️ 这里**没有** `created_by == 我` 那一条 —— 那属于 private 分支。
        ((model.visibility == "workspace")
         & model.workspace_id.in_(workspace_ids))
        if workspace_ids else false(),
        # private（含"任何非 global/workspace 的取值"）：只有创建者。
        # ⚠️ 这里**故意**写成 `!= 'workspace'` 而不是 `== 'private'`，
        # 与 `can_write_kb` 的 `else` 分支逐字对齐 —— 可见性枚举将来加第四档时，
        # 两边仍然是同一个判据（改一处就要改另一处，所以它们在同一个文件里）。
        ((model.visibility != "workspace") & (model.created_by == user_id)),
    ]
    # 🔴 D130：工作区还软删着 → 恢复不了它的库（先恢复工作区）。
    #   同样必须 **AND** 在整个 or_ 外面（理由见 `_visible_kb_clause`）。
    return _with_workspace(
        and_(or_(*conds), _workspace_alive(model, workspace_model)),
        model,
        workspace_id,
    )


def writable_kb_clause(ctx: UserContext, *, model=None, workspace_model=None,
                       workspace_id: int | None = None):
    """"**我能改**的 KB"这个条件（PG 侧，回收站列表用）。

    🔑 它与 `visible_kb_clause` **不是同一件事**，别混用（D129 实测踩到）：

    | | global | workspace | private |
    |---|---|---|---|
    | 读（`visible_kb_clause`） | 所有人 | 成员 **或创建者** | 创建者 |
    | 写（`can_write_kb` / 本函数） | 所有人 | **只有成员** | 创建者 |

    差在"workspace 可见性但创建者已不是成员"这一格：读判定里
    `created_by == 我` 是一条**独立**的 `or` 分支，所以那种库**读得到**；
    而 `can_write_kb` 是**按 visibility 分派**的 —— 它不看 `created_by`。

    ⚠️ 为什么要单独写一个：回收站列表必须与**恢复**的判据一致。
    混用读判据的后果是"列表里有、点恢复 403"（D129 第一版就是这个）。
    两者的等价关系由 `tests/test_container_trash.py` 的**真值表**钉住
    （`can_write_kb` 的 Python 实现 vs 本函数的 SQL，在真实 PG 上逐格比对）。

    @param model / @param workspace_id 同 `visible_kb_clause`
    """
    if ctx.is_superadmin:
        # 🔑 D173：超管能改所有库。
        return _with_workspace(true(), _resolve_kb_model(model), workspace_id)
    return _writable_kb_clause(
        ctx.user_id, ctx.workspace_ids, model=model, workspace_model=workspace_model,
        workspace_id=workspace_id,
    )


async def load_user_context(
    session: AsyncSession, plaintext_key: str
) -> UserContext:
    """校验 API Key 并推导权限范围。

    失败一律抛 `InvalidToken` —— 不区分"key 不存在 / 已吊销 / 已过期"，
    同样是阻断存在性探测。
    """
    from .models import ApiKey, KnowledgeBase, User, WorkspaceMember

    key_hash = hash_key(plaintext_key)
    stmt = (
        select(ApiKey, User)
        .join(User, User.id == ApiKey.user_id)
        .where(
            ApiKey.key_hash == key_hash,
            ApiKey.revoked_at.is_(None),
            User.is_active.is_(True),
        )
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise InvalidToken()

    api_key, user = row
    now = dt.datetime.now(dt.UTC)
    if api_key.expires_at and api_key.expires_at < now:
        raise InvalidToken("API Key 已过期")

    # 🔑 D173：全局角色从**用户行**读（与 API Key 一样是服务端推导，客户端不可指定）。
    is_superadmin = getattr(user, "role", "user") == "superadmin"

    # 推导权限范围：一次查询拿全，避免后续多次往返
    if is_superadmin:
        # 超管：所有工作区 + 所有未删的库。下游（Qdrant filter / SQL scope /
        # can_read_kb / can_write_kb）都读这两个集合，所以一处展开、处处生效。
        ws_rows = await session.execute(select(WorkspaceMember.workspace_id))
        workspace_ids = frozenset(ws_rows.scalars().all())
        kb_rows = await session.execute(
            select(KnowledgeBase.id).where(KnowledgeBase.deleted_at.is_(None))
        )
        readable_kb_ids = frozenset(kb_rows.scalars().all())
    else:
        ws_rows = await session.execute(
            select(WorkspaceMember.workspace_id).where(
                WorkspaceMember.user_id == user.id
            )
        )
        workspace_ids = frozenset(ws_rows.scalars().all())

        # 可读 KB 范围。⚠️ 三档可见性由 `_visible_kb_clause` 独家实现
        # （CONVENTIONS §0.6 / D129）—— 这里**不要**再写一遍：
        # 本项目唯一一次真实越权就是"两处复刻可见性逻辑"造成的，
        # 而这份逻辑本来就有两份拷贝，再加第三份（回收站）只会开始漂。
        #
        # 🔑 这里调**零件版**（`user_id` + `workspace_ids`）而不是收 ctx 的那个：
        # 可读范围是 ctx 的**一个字段**，而 ctx 要等它算完才能建 ——
        # `UserContext` 是 frozen dataclass，"先建后填"会 `FrozenInstanceError`，
        # "不先建就传 ctx"会 `UnboundLocalError`。两个都是**每个请求 500**，
        # 而单测测不到（它们不需要真 API Key，§5.122）。
        kb_rows = await session.execute(
            select(KnowledgeBase.id).where(
                KnowledgeBase.deleted_at.is_(None),
                _visible_kb_clause(user.id, workspace_ids, model=KnowledgeBase),
            )
        )
        readable_kb_ids = frozenset(kb_rows.scalars().all())

    # 🔑 记录使用时间。⚠️ 注释原来写的是"异步，不阻塞本次请求"，**那是假的**：
    #   这只是一次同步的 ORM 属性赋值，没有后台任务、没有独立事务。
    #   它之所以会落库，是因为请求作用域的 session 在**响应前**无条件
    #   `commit()`（`deps.get_session` 的 `finally`）——
    #   实测：只读请求（`GET /kb`）之后 `last_used_at` 确实更新了。
    #   ⚠️ 推论：**失败请求（4xx/5xx）不会留下使用时间**，因为那条路径走
    #   `rollback`。所以它的语义是"最后一次**成功**使用"，字段注释别写错。
    api_key.last_used_at = now

    ctx = UserContext(
        user_id=user.id,
        display_name=user.display_name,
        email=user.email,
        api_key_id=api_key.id,
        scopes=frozenset(api_key.scopes or ()),
        workspace_ids=workspace_ids,
        readable_kb_ids=readable_kb_ids,
        is_superadmin=is_superadmin,
        nickname=getattr(user, "nickname", None),
    )
    return ctx


# ============================================================
# Qdrant 过滤构造 —— 唯一入口
# ============================================================


def build_qdrant_filter(
    ctx: UserContext, kb_id: int | None = None, *, kb_ids: frozenset[int] | set[int] | list[int] | None = None
) -> dict[str, Any]:
    """构造 Qdrant 过滤条件。

    **fail-closed 语义**：拿不到授权范围就返回必然空集的 filter，
    绝不能因为"没查到"就退化成不过滤（那等于全库可读）。

    三档可见性：
    - private  → 只有 `created_by == ctx.user_id` 的（服务端在 PG 侧解析成 id 集合）
    - workspace → `workspace_id ∈ ctx.workspace_ids`
    - global  → 所有用户（D27/D43）

    @param kb_id 指定单个 KB；None 表示"该用户有权限的所有 KB"
    @param kb_ids 指定多个 KB（跨库搜索）。⚠️ 它**只是意图**，
        实际范围是 `ctx.readable_kb_ids ∩ kb_ids` —— 求交只会更小，
        所以传一个无权限的 kb_id 进来**不会越权，只会少结果**。
        （早先的实现在这里传 `kb_id=None`，于是"搜 [1,2]"会搜成
        "搜所有有权限的库"，包括用户没要求的 3 号 —— 是真实的越权读取。）
    """
    allowed = ctx.readable_kb_ids
    must: list[dict[str, Any]] = []

    if kb_id is not None:
        # 指定了 KB，必须在授权集合内，否则返回空集（不是报错，
        # 报错会泄漏"这个 kb_id 存在"）
        if kb_id not in allowed:
            return _empty_filter()
        must.append({"key": "kb_id", "match": {"value": kb_id}})
    else:
        want = allowed if kb_ids is None else (allowed & set(kb_ids))
        if not want:
            return _empty_filter()
        must.append({"key": "kb_id", "match": {"any": sorted(want)}})

    return {"must": must}


# 空的恒假值。KB id 是 BIGSERIAL 且从 1 开始，所以 -1 永远不存在 ——
# 拿它当"必然空集"的哨兵，语义上和 Qdrant 侧的 `{"kb_id": -1}` 一致。
NO_SUCH_KB = -1


def _empty_filter() -> dict[str, Any]:
    """必然空集的 Qdrant filter。

    单独抽出来是因为它出现三次（`build_qdrant_filter` 两个分支 +
    `build_kb_scope_sql` 的 SQL 版），而**它的含义很反直觉**：
    `kb_id == -1` 看起来像"限制 kb_id"，实际是"什么都不要"。
    忘了 fail-closed 分支就是全库可读，所以给它一个名字。
    """
    return {"must": [{"key": "kb_id", "match": {"value": NO_SUCH_KB}}]}


def build_kb_scope_sql(
    ctx: UserContext,
    kb_id: int | None = None,
    column: Any = None,
    *,
    kb_ids: frozenset[int] | set[int] | list[int] | None = None,
) -> Any:
    """构造 **Postgres 侧**的租户范围条件（`docs/DESIGN-API.md` §5.2 的 trgm 腿用）。

    ## 为什么需要它

    硬约束 `CONVENTIONS §0.2` 说"租户过滤只有一个入口 `build_qdrant_filter`"。
    但 `/kb/{id}/search` 的 `mode=hybrid` 有一路在 **Postgres** 里跑 trgm 子串搜索
    （D17/D40：BM25 留 Qdrant，trgm 留 Postgres），它拿不到、也不该拿 Qdrant 的 filter 字典。

    如果让 `retrieval/trgm.py` 自己 `join(KnowledgeBase)` 复刻一遍三档可见性，
    就等于**开了第二个租户过滤入口** —— 而本项目唯一一次真实越权（CONVENTIONS §0.6）
    正是"两处复刻可见性逻辑"造成的。所以这里补上 SQL 版本，与 Qdrant 版本**同源**：

        唯一真相源 = `ctx.readable_kb_ids`（`load_user_context` 从 API Key 推导，
        客户端不可指定，D8/§0.5）

    两个函数都只读这一个集合，不各自重算可见性。

    ## fail-closed

    与 `build_qdrant_filter` 完全一致：拿不到授权范围就返回 `column == -1`
    （恒假），**绝不**退化成"不过滤"。

    @param column 比较用的列，默认 `KnowledgeBase.id`。调用方要比较别的列时
        （例如已经 join 过了 `KnowledgeBase.id` 就直接传它）传进来即可。
    @param kb_ids 与 `build_qdrant_filter` 同义：`ctx.readable_kb_ids ∩ kb_ids`。
    """
    from .models import KnowledgeBase

    col = KnowledgeBase.id if column is None else column
    allowed = ctx.readable_kb_ids

    if kb_id is not None:
        return col == kb_id if kb_id in allowed else col == NO_SUCH_KB
    want = allowed if kb_ids is None else (allowed & set(kb_ids))
    if not want:
        return col == NO_SUCH_KB
    return col.in_(sorted(want))


def can_write_kb(ctx: UserContext, kb_row: Any) -> bool:
    """能否修改这个 KB（**行**版本；列表/回收站用 `writable_kb_clause` 的 SQL 版）。

    D4 暂不做 RBAC，D43 定了"全局公共库所有用户可读可编辑"，
    所以这里的规则是：
      - global    → 任何用户（D43）
      - workspace → **该工作区的成员**（⚠️ 不看 `created_by`，见 `writable_kb_clause`
                     的表：创建者但已不是成员时读得到、改不了）
      - 其它(private) → 只有创建者

    🔑 **这一份与 `writable_kb_clause` 必须逐档等价**：
    `restore_kb` 用的是这个函数，回收站列表用的是那个 SQL 条件 ——
    不等价就会出现"列表里有、点恢复 403"。
    `tests/test_container_trash.py` 的真值表在真实 PG 上逐格比对两者。
    """
    if ctx.is_superadmin:
        # 🔑 D173：超管能改所有库（与 `writable_kb_clause` 的超管分支逐档等价）。
        return True
    vis = getattr(kb_row, "visibility", None)
    if vis == "global":
        return True
    if vis == "workspace":
        return getattr(kb_row, "workspace_id", None) in ctx.workspace_ids
    return getattr(kb_row, "created_by", None) == ctx.user_id


async def assert_kb_write(
    session: AsyncSession, ctx: UserContext, kb_id: int
) -> Any:
    """PG 侧的写权限断言。返回 KB 行，省一次查询。

    失败抛 `ResourceNotFound` —— 与读权限**同一个错误码**（fail-closed）：
    调用方不能通过"写入被拒 vs 读取被拒"的差异推断资源是否存在。
    """
    from .models import KnowledgeBase

    row = (
        await session.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.id == kb_id,
                KnowledgeBase.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()

    if row is None or not can_write_kb(ctx, row):
        raise ResourceNotFound(detail={"kb_id": kb_id})
    return row


async def assert_kb_access(
    session: AsyncSession, ctx: UserContext, kb_id: int
) -> None:
    """PG 侧的资源访问断言。

    失败抛 `ResourceNotFound`（403 / KB_NOT_FOUND），
    与 Qdrant 侧的空集过滤保持**同一种对外表现**。
    """
    from .models import KnowledgeBase

    stmt = select(KnowledgeBase.id).where(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.deleted_at.is_(None),
    )
    exists = (await session.execute(stmt)).scalar_one_or_none()
    if exists is None or not ctx.can_read_kb(kb_id):
        raise ResourceNotFound(detail={"kb_id": kb_id})


# ============================================================
# 工作区访问
# ============================================================


def can_admin_workspace(ctx: UserContext, ws_row: Any) -> bool:
    """能否管理工作区（改设置、增删成员、删工作区）。

    D4 暂不做 RBAC，所以只有 owner 能管理，member 只能读。
    未来的角色表加在这里 —— 别在路由里散落判断。

    🔑 D173：超级管理员能管**任何**工作区。
    """
    if ctx.is_superadmin:
        return True
    return getattr(ws_row, "owner_id", None) == ctx.user_id


def assert_superadmin(ctx: UserContext) -> None:
    """断言当前用户是超级管理员（D173）。

    失败抛 `Forbidden`（403 / `FORBIDDEN`）—— 这里是**操作级**拒绝，
    不是"资源不存在"，所以与 `ResourceNotFound` 的 fail-closed 语义不同
    （见 `errors.Forbidden` 的说明）。
    """
    if not ctx.is_superadmin:
        raise Forbidden("只有超级管理员能执行该操作")


async def assert_workspace_access(
    session: AsyncSession, ctx: UserContext, ws_id: int
) -> Any:
    """断言用户是该工作区成员，返回工作区行。"""
    from .models import Workspace

    row = (
        await session.execute(
            select(Workspace).where(
                Workspace.id == ws_id, Workspace.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()

    # fail-closed：非成员 → 与"不存在"同一个错误码
    if row is None or ws_id not in ctx.workspace_ids:
        raise ResourceNotFound(detail={"workspace_id": ws_id})
    return row


async def assert_workspace_admin(
    session: AsyncSession, ctx: UserContext, ws_id: int
) -> Any:
    """断言用户是该工作区 owner。"""
    row = await assert_workspace_access(session, ctx, ws_id)
    if not can_admin_workspace(ctx, row):
        # 同样 fail-closed：不能通过错误码区分"是成员但不是 owner"
        # 与"根本不是成员" —— 两者都返回同一个 KB_NOT_FOUND
        raise ResourceNotFound(detail={"workspace_id": ws_id})
    return row


def hash_password(password: str) -> str:
    """密码哈希（argon2id）。

    与 `hash_key` 刻意不同：密码熵低必须用慢哈希，API Key 是随机数不需要。
    """
    from argon2 import PasswordHasher

    return PasswordHasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError

    try:
        return PasswordHasher().verify(password_hash, password)
    except VerifyMismatchError:
        return False
