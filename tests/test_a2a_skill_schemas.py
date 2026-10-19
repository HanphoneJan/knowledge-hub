"""🔴 D145：Agent Card 声明**可机器读**的输入/输出，并与**真实响应一致**。

## 缺口

M11 写的是「Agent Card **声明能力与输入输出**」。实测 Agent Card 里每个 skill 只有
`inputModes` / `outputModes`（**媒体类型**）与 `description` / `tags` / `examples`，
**没有任何机器可读的 schema**。

⚠️ A2A 规范本身**不要求** skill 带 schema（`AgentSkill` 只有
id/name/description/tags/examples）—— 所以这不是"不合规范"，而是**需求没落地**：
外部 Agent 只能从 `description` 里猜参数名，猜错时拿到的是

```
400 {"error":{"code":"INVALID_REQUEST","message":"query/message 不能为空"}}
```

而这个端点**刻意**不在 Card 里列出可用的 `kb_id`（那是有权限信息的泄露），
于是调用方连"我该传哪个 kb_id"都无法从 Card 里知道。

## 这一组钉的最要紧的一条

⚠️ **声明必须与真实响应一致**，否则它就是**第二份契约**（会漂）。
我写这份 `outputSchema` 的**第一稿就错了**：声明成
`{answer: str, sources: []}`，而真实形状是
`{name, parts: [{kind, text, metadata}], sources}` ——
正文在 `parts[0].text`（A2A 规范的 `artifacts[].parts[]` 位置），
**没有**顶层 `answer`。

⇒ 所以这里不只测"schema 存在"，而是**拿真实调用**的 artifact
**逐字段核对**声明的 `required` 与 `properties`。
"""

from __future__ import annotations

import time

import pytest

from services.api.routers.a2a import SKILLS, SKILL_IDS

SKILL_BY_ID = {s["id"]: s for s in SKILLS}


# ============================================================
# 1. 声明本身
# ============================================================


def test_every_skill_declares_input_and_output_schema():
    """🔑 每个 skill 都要有 `inputSchema` 与 `outputSchema`。"""
    for sid, skill in SKILL_BY_ID.items():
        assert "inputSchema" in skill, f"{sid} 没有 inputSchema"
        assert "outputSchema" in skill, f"{sid} 没有 outputSchema"
        assert skill["inputSchema"].get("type") == "object", sid
        assert skill["outputSchema"].get("type") == "object", sid


def test_input_schema_marks_kb_id_required():
    """🔑 `kb_id` **必填**，且说明为什么不列可用值。"""
    for sid, skill in SKILL_BY_ID.items():
        req = skill["inputSchema"].get("required") or []
        assert "kb_id" in req, f"{sid} 的 required 里没有 kb_id：{req}"
        desc = skill["inputSchema"]["properties"]["kb_id"].get("description") or ""
        assert "必填" in desc, desc


def test_skill_enum_matches_the_registered_skills():
    """🔑 声明里的 `skill` enum == 真实支持的 skill 集合（别只写 answer）。"""
    enum = SKILL_BY_ID["answer"]["inputSchema"]["properties"]["skill"]["enum"]
    assert set(enum) == SKILL_IDS, (enum, SKILL_IDS)


# ============================================================
# 2. 声明 vs **真实响应**（防漂的关键）
# ============================================================


def _check_against(value: dict, schema: dict, path: str = "") -> list[str]:
    """按声明校验一个真实 artifact，返回**问题列表**。

    🔑 只实现"这份声明真的用到的"关键字（`type` / `required` /
    `properties` / `const` / `items`）—— 校验器与声明同源，才有意义；
    引入 jsonschema 又多一份"升级后行为变了"的风险（理由同 D144）。
    """
    problems: list[str] = []
    want = schema.get("type")
    # 🔑 类型检查要**覆盖标量**：第一版只判 object/array，于是
    #   `{"sources": {"type": "string"}}` 这种声明错了也照样全绿
    #   （变异脚本抓出来的）。`want` 可能是字符串**或列表**（`["array","null"]`）。
    wants = [want] if isinstance(want, str) else list(want or ())
    actual = type(value).__name__
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if wants:
        preds = [checks[w] for w in wants if w in checks]
        if preds and not any(pred(value) for pred in preds):
            return [f"{path or '<root>'} 声明类型 {wants}，实际 {actual}"]

    if isinstance(value, dict):
        for key in schema.get("required") or ():
            if key not in value:
                problems.append(f"{path or '<root>'}.{key} 声明必填但真实响应里没有")
        props = schema.get("properties") or {}
        for key, spec in props.items():
            if key in value:
                problems += _check_against(value[key], spec, f"{path}.{key}".lstrip("."))
        if "const" in schema and value.get("name") != schema["const"]:
            problems.append(
                f"{path or '<root>'}.name 声明固定为 {schema['const']!r}，"
                f"实际 {value.get('name')!r}"
            )
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            problems += _check_against(item, schema["items"], f"{path}[{i}]")
    return problems


def _undeclared(value: dict, schema: dict, path: str = "") -> list[str]:
    """**反向**检查：真实响应里有、而声明里**没有**的字段。

    🔑 `_check_against` 是单向的（"声明的必填字段都在"）——
    变异脚本立刻证明了它的盲区：把 `required` 从 6 项删到 3 项**照样全绿**，
    因为那 3 项确实都在响应里。而"多出来的字段"同样要报：
    响应里加了一个 `usage` 字段而 Card 不提，外部客户端就**发现不了它**
    （而这正是"声明会漂"的另一半）。

    ⚠️ 只在 schema **声明了 properties** 的那一层查 —— 对 `{"type":"object"}`
    这种开放对象（`history[]` 的元素、`parts[].metadata`）不查，
    否则每加一个元数据字段都会红。
    """
    if not isinstance(value, dict) or not isinstance(schema.get("properties"), dict):
        return []
    props = schema["properties"]
    problems = [
        f"{path or '<root>'}.{k} 真实响应里有、但声明里没有"
        for k in value if k not in props
    ]
    for key, spec in props.items():
        if key in value and isinstance(spec, dict):
            problems += _undeclared(value[key], spec, f"{path}.{key}".lstrip("."))
    return problems


@pytest.fixture(scope="module")
def _env(purge_users):
    """真账号 + 真库 + 一份真文档（A2A 需要 `kb_id`，且要有内容才能返回 sources）。"""
    import httpx

    c = httpx.Client(base_url="http://127.0.0.1:8010", trust_env=False, timeout=90)
    mail = f"a2a-d145-{time.time():.0f}@t.local"
    r = c.post("/api/v1/auth/register",
               json={"email": mail, "password": "pw12345678", "display_name": "x"})
    if r.status_code != 201:
        pytest.skip(f"注册失败（API 没起？）：HTTP {r.status_code}")
    key = r.json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}
    ws = c.post("/api/v1/workspaces", headers=h,
                json={"name": "d145", "visibility": "private"}).json()
    kb = c.post("/api/v1/kb", headers=h,
                json={"workspace_id": ws["id"], "name": "d145kb",
                      "visibility": "private"}).json()
    c.post(f"/api/v1/kb/{kb['id']}/documents", headers=h,
           files={"files": ("note.txt",
                            "红富士是苹果的红色品种，产自山东。\n红玉also。\n".encode(),
                            "text/plain")})
    time.sleep(10)          # 等摄取把 chunk 写进去（没有它 sources 会是空的）
    yield c, h, kb["id"], mail
    # 🔑 清场**必须**重建 BM25（`conftest.purge_users`）：这个 fixture **真的
    #   摄取过文档**（有 chunk 落库），裸 `DELETE FROM users` 会级联删掉
    #   chunks 而不动 df ⇒ 下一次 pytest 假红在
    #   `test_corpus_stats_matches_live_chunks`（§5.109、D146）。
    #   而且重建顺带清掉 Qdrant 的孤儿点（硬删用户留下的）。
    purge_users([mail])


@pytest.mark.parametrize("skill", sorted(SKILL_IDS))
def test_declared_output_schema_matches_real_response(_env, skill):
    """🔑 **声明的 `outputSchema` 与真实 artifact 逐字段一致**。

    ⚠️ 这条是第一稿就红的那条：我声明成 `{answer, sources}`，
    而真实形状是 `{name, parts[], sources}`。
    只测"schema 存在"的守卫抓不到这种错 —— 必须拿**真响应**比。
    """
    c, h, kb_id, _mail = _env
    r = c.post("/a2a/tasks", headers=h,
               json={"skill": skill, "query": "苹果的红色品种", "kb_id": kb_id})
    assert r.status_code == 200, r.text[:200]
    task = r.json()
    assert task["status"]["state"] == "completed", task.get("status")
    # 🔑 比**整个响应体**：声明描述的是调用方真正收到的 Task
    #   （正文在 `artifacts[].parts[].text`，来源在 `metadata.sources`）。
    schema = SKILL_BY_ID[skill]["outputSchema"]
    problems = _check_against(task, schema) + _undeclared(task, schema)
    assert problems == [], (
        f"{skill} 的 outputSchema 与真实响应不符：{problems}\n"
        f"真实响应顶层键：{sorted(task)}"
    )


@pytest.mark.parametrize("skill", sorted(SKILL_IDS))
def test_declared_input_schema_accepts_what_the_endpoint_actually_takes(_env, skill):
    """🔑 声明的 `inputSchema` 覆盖**真实可用的入参**（skill / query / kb_id）。"""
    c, h, kb_id, _mail = _env
    r = c.post("/a2a/tasks", headers=h,
               json={"skill": skill, "query": "苹果", "kb_id": kb_id})
    assert r.status_code == 200, r.text[:200]
    schema = SKILL_BY_ID[skill]["inputSchema"]
    sent = {"skill": skill, "query": "苹果", "kb_id": kb_id}
    assert schema.get("additionalProperties") is False, (
        "声明说'不许额外参数'，那它就该真的一个都不多给"
    )
    unknown = set(sent) - set(schema["properties"])
    assert not unknown, f"声明里缺这些真实可用的参数：{sorted(unknown)}"


def test_declared_required_matches_reality_missing_kb_id_is_rejected(_env):
    """🔑 声明 `kb_id` 必填 ⇒ 真的**不给就会失败**（否则声明是空约束）。"""
    c, h, _kb_id, _mail = _env
    r = c.post("/a2a/tasks", headers=h, json={"skill": "answer", "query": "苹果"})
    assert r.status_code == 400, r.text[:200]
    assert "kb_id" in r.json()["error"]["message"], r.json()