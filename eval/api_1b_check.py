"""1b 子步骤的端到端 HTTP 验证。

用 httpx 直连跑真实请求，验证路由行为而不是靠肉眼看 OpenAPI 文档。
覆盖的重点是**权限边界**（fail-closed 是否真的成立），
而不是"接口能返回 200"。

跑法：服务起在 127.0.0.1:8010，然后
    uv run python eval/api_1b_check.py
"""

from __future__ import annotations

import os

# 🔴 离线加载模型（说明见 tests/conftest.py）：不设的话每次加载 bge/colSmol
#   都会去 huggingface.co 重试 5 次（≈23s），整套 eval 慢到几十分钟。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import json
import sys
import time
from pathlib import Path

import httpx

# 项目根要在 sys.path 里才能 `from services...`（services/ 是源码目录不是安装包）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 🔑 `eval/` 自己也要在路径上 —— `_wipe_test_data` 用 `_reset.reset_users_and_bm25`
#   （CONVENTIONS §9：清理必须走那一条，见那里的说明）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = os.environ.get("PKH_TEST_BASE", "http://127.0.0.1:8010")
API = f"{BASE}/api/v1"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"

_fail: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        _fail.append(label)


def section(t: str) -> None:
    print(f"\n\033[1m{t}\033[0m")


TEST_USERS = [
    ("alice@t.local", "Alice", "alice-pass-123"),
    ("bob@t.local", "Bob", "bob-pass-12345"),
    ("carol@t.local", "Carol", "carol-pass-12"),
]


def _wipe_test_data() -> None:
    """清掉上一轮遗留的测试账号，**并重建 BM25**。

    注册端点没有"注销"（D3 只有 key 吊销），所以只能直接删库。
    脚本**自己造的数据自己清** —— 别去动别人的账号。

    🔴 必须走 `eval/_reset.py::reset_users_and_bm25`，**不能**只
    `DELETE FROM users`（CONVENTIONS §9 明写）：硬删用户会级联删掉
    `chunks`，而 BM25 的 `terms.df` / `corpus_stats.n_docs` **只在入库与
    cleanup 里维护**（§2.7）→ df 虚高。
    第一版就是裸 DELETE，于是：**下一次跑 pytest 会红**，
    报「n_docs=507 但只有 501 个 chunk 行，跑 handle_reindex 重建」——
    而真正的原因是"上一轮 1b 自己清了库没重建 df"，
    照那条提示去跑 reindex 会发现**重建前后一样红**（§5.109）。
    """
    from _reset import psql, reset_users_and_bm25

    # 🔑 按**域名后缀**收集，而不是三个固定邮箱。
    # ⚠️ 脚本自己还会造带时间戳的账号（`carol-3rd-*`、`ratelimit-*`…），
    #   固定列表漏了它们 ⇒ 每跑一轮就往库里多留几个用户，
    #   而"下次跑会拿到 409 已注册"这个症状完全指不到"清理列表漏了它"
    #   （第一版就是这么漏的，§5.114 的老坑）。
    #   `@t.local` 是本脚本专属域（真人账号不用它），删它不碰别人的数据。
    emails = [
        e.strip() for e in psql(
            "SELECT email FROM users WHERE email LIKE '%@t.local'"
        ).splitlines() if e.strip()
    ] or ["alice@t.local", "bob@t.local", "carol@t.local"]
    try:
        stats = reset_users_and_bm25(emails)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 清场失败（继续）：{exc}")
        return
    print(f"清场：{len(emails)} 个账号 · n_docs={stats['n_docs']} "
          f"chunk 行={stats['real_chunks']}")



def _setting(name: str) -> bool:
    """读**本机** `Settings` 的某个开关（eval 与 API 同机、同 `.env`）。

    ⚠️ 为什么要读而不是写死期望值：这两个开关是**部署选择**
    （`.env` 里的 `ALLOW_REGISTER` / `EXPOSE_DOCS`），
    把"开着"也报成失败 ⇒ 换个部署方式基线就红，而它其实没坏。
    """
    try:
        from services.common.config import get_settings

        return bool(getattr(get_settings(), name))
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 读 Settings.{name} 失败（按'开着'处理）：{exc}")
        return True


def _allow_register_setting() -> bool:
    return _setting("allow_register")


def _expose_docs_setting() -> bool:
    return _setting("expose_docs")

def main() -> int:
    # trust_env=False：本机有代理，会把内网请求送出去
    c = httpx.Client(trust_env=False, timeout=30, base_url=BASE)

    _wipe_test_data()

    section("1. 注册")
    r = c.post(
        f"{API}/auth/register",
        json={"email": "alice@t.local", "password": "alice-pass-123",
              "display_name": "Alice"},
    )
    check("注册返回 201", r.status_code == 201, f"HTTP {r.status_code}")
    if r.status_code != 201:
        print(r.text[:400])
        return 1
    alice = r.json()
    K_A = alice["api_key"]
    check("返回明文 api_key", K_A.startswith("sk-pkh_"), K_A[:20] + "...")
    check("返回 user 信息", alice["user"]["email"] == "alice@t.local")
    WS_A = None

    r = c.post(
        f"{API}/auth/register",
        json={"email": "bob@t.local", "password": "bob-pass-12345",
              "display_name": "Bob"},
    )
    K_B = r.json()["api_key"]
    check("第二个用户注册成功", r.status_code == 201, f"HTTP {r.status_code}")

    r = c.post(
        f"{API}/auth/register",
        json={"email": "carol@t.local", "password": "carol-pass-12",
              "display_name": "Carol"},
    )
    K_C = r.json()["api_key"]
    check("第三个用户注册成功", r.status_code == 201)
    b_id = r.json()["user"]["id"]

    section("2. 鉴权边界（D8 fail-closed）")
    r = c.get(f"{API}/auth/me")
    check("无 Authorization → 401", r.status_code == 401, f"HTTP {r.status_code}")
    check("  错误码是 INVALID_TOKEN", r.json()["error"]["code"] == "INVALID_TOKEN")

    r = c.get(f"{API}/auth/me", headers={"Authorization": "Bearer sk-pkh_bogus_key"})
    check("坏 key → 401", r.status_code == 401)
    check("  与'无 key'同码（不可区分）",
          r.json()["error"]["code"] == "INVALID_TOKEN")

    r = c.get(f"{API}/auth/me", headers={"Authorization": "Basic abc"})
    check("非 Bearer 方案 → 401", r.status_code == 401)

    r = c.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {K_A}"})
    check("合法 key → 200", r.status_code == 200)
    check("  返回正确的用户", r.json()["email"] == "alice@t.local")

    section("3. 工作区")
    r = c.get(f"{API}/workspaces", headers={"Authorization": f"Bearer {K_A}"})
    check("注册后自动有工作区", r.status_code == 200 and len(r.json()) == 1,
          f"{len(r.json())} 个")
    WS_A = r.json()[0]["id"]
    check("  名字带用户名", "Alice" in r.json()[0]["name"], r.json()[0]["name"])
    check("  是自己 owner", r.json()[0]["owner_id"] == alice["user"]["id"])

    r = c.get(f"{API}/workspaces/{WS_A}", headers={"Authorization": f"Bearer {K_B}"})
    check("非成员访问他 workspace → 403 KB_NOT_FOUND",
          r.status_code == 403 and r.json()["error"]["code"] == "KB_NOT_FOUND",
          f"HTTP {r.status_code} {r.json().get('error',{}).get('code')}")

    r = c.get(f"{API}/workspaces/99999", headers={"Authorization": f"Bearer {K_A}"})
    check("访问不存在 workspace → 同码 403",
          r.status_code == 403 and r.json()["error"]["code"] == "KB_NOT_FOUND",
          "与'无权'不可区分")

    r = c.post(
        f"{API}/workspaces", headers={"Authorization": f"Bearer {K_A}"},
        json={"name": "Alice 第二个工作区", "description": "测试"},
    )
    check("创建工作区 → 201", r.status_code == 201, f"HTTP {r.status_code}")
    WS_A2 = r.json()["id"]
    check("  自动成为 owner", r.json()["owner_id"] == alice["user"]["id"])

    r = c.patch(
        f"{API}/workspaces/{WS_A2}", headers={"Authorization": f"Bearer {K_A}"},
        json={"name": "改名成功"},
    )
    check("owner 改设置 → 200", r.status_code == 200 and r.json()["name"] == "改名成功")

    section("4. 成员管理")
    r = c.post(
        f"{API}/workspaces/{WS_A}/members",
        headers={"Authorization": f"Bearer {K_A}"},
        json={"email": "bob@t.local", "role": "member"},
    )
    check("owner 加成员 → 201", r.status_code == 201, f"HTTP {r.status_code}")
    check("  角色是 member", r.json().get("role") == "member")

    r = c.post(
        f"{API}/workspaces/{WS_A}/members",
        headers={"Authorization": f"Bearer {K_A}"},
        json={"email": "bob@t.local", "role": "member"},
    )
    check("重复加成员 → 400", r.status_code == 400, f"HTTP {r.status_code}")

    r = c.post(
        f"{API}/workspaces/{WS_A}/members",
        headers={"Authorization": f"Bearer {K_A}"},
        json={"email": "nobody@t.local"},
    )
    check("邀请未注册用户 → 400 并说明原因", r.status_code == 400,
          r.json()["error"]["message"][:40])

    r = c.post(
        f"{API}/workspaces/{WS_A}/members",
        headers={"Authorization": f"Bearer {K_A}"},
        json={"email": "carol@t.local", "role": "owner"},
    )
    check("拒绝授予 owner → 400", r.status_code == 400,
          r.json()["error"]["message"][:30])

    # Bob 现在是 member
    r = c.get(f"{API}/workspaces/{WS_A}", headers={"Authorization": f"Bearer {K_B}"})
    check("member 能读工作区", r.status_code == 200)

    r = c.patch(
        f"{API}/workspaces/{WS_A}", headers={"Authorization": f"Bearer {K_B}"},
        json={"name": "member 改名"},
    )
    check("member 改设置 → 403 KB_NOT_FOUND（与不存在同码）",
          r.status_code == 403 and r.json()["error"]["code"] == "KB_NOT_FOUND",
          f"HTTP {r.status_code}")

    r = c.get(f"{API}/workspaces/{WS_A}/members",
              headers={"Authorization": f"Bearer {K_B}"})
    check("member 能看成员列表", r.status_code == 200 and len(r.json()) == 2,
          f"{len(r.json())} 人")

    section("5. 知识库 CRUD")
    r = c.post(
        f"{API}/kb", headers={"Authorization": f"Bearer {K_A}"},
        json={"workspace_id": WS_A, "name": "Alice 私有库", "visibility": "private"},
    )
    check("建 private KB → 201", r.status_code == 201, f"HTTP {r.status_code}")
    KB_PRIV = r.json()["id"]
    check("  embed_model 自动填全局统一值",
          r.json()["embed_model"] == "BAAI/bge-small-zh-v1.5")

    r = c.post(
        f"{API}/kb", headers={"Authorization": f"Bearer {K_A}"},
        json={"workspace_id": WS_A, "name": "团队共享库", "visibility": "workspace"},
    )
    KB_WS = r.json()["id"]
    check("建 workspace KB → 201", r.status_code == 201)

    r = c.post(
        f"{API}/kb", headers={"Authorization": f"Bearer {K_A}"},
        json={"workspace_id": WS_A, "name": "全局公共库", "visibility": "global"},
    )
    KB_GLOBAL = r.json()["id"]
    check("建 global KB → 201", r.status_code == 201)

    r = c.post(
        f"{API}/kb", headers={"Authorization": f"Bearer {K_A}"},
        json={"workspace_id": WS_A, "name": "非法分块", "chunk_size": 100,
              "chunk_overlap": 200},
    )
    check("overlap>=size → 400", r.status_code == 400,
          r.json()["error"]["message"][:40])

    r = c.post(
        f"{API}/kb", headers={"Authorization": f"Bearer {K_A}"},
        json={"workspace_id": 99999, "name": "越权建库"},
    )
    check("往不存在的工作区建库 → 403", r.status_code == 403,
          f"HTTP {r.status_code}")

    section("6. 可见性隔离（D6 / D43）")
    hB = {"Authorization": f"Bearer {K_B}"}
    hC = {"Authorization": f"Bearer {K_C}"}

    r = c.get(f"{API}/kb/{KB_PRIV}", headers=hB)
    check("Bob 读 Alice private KB → 403 KB_NOT_FOUND",
          r.status_code == 403 and r.json()["error"]["code"] == "KB_NOT_FOUND")

    r = c.get(f"{API}/kb/{KB_WS}", headers=hB)
    check("Bob（同工作区）读 workspace KB → 200", r.status_code == 200)

    r = c.get(f"{API}/kb/{KB_GLOBAL}", headers=hC)
    check("Carol（无共同工作区）读 global KB → 200", r.status_code == 200,
          "D43: 全局库所有人可读")

    r = c.patch(f"{API}/kb/{KB_GLOBAL}", headers=hC, json={"description": "Carol 改的"})
    check("Carol 改 global KB → 200（D43 所有人可编辑）", r.status_code == 200)

    r = c.patch(f"{API}/kb/{KB_PRIV}", headers=hB, json={"name": "Bob 改的"})
    check("Bob 改 Alice private KB → 403", r.status_code == 403)

    r = c.get(f"{API}/kb", headers=hC)
    names = [k["name"] for k in r.json()]
    check("Carol 列表不含 Alice private KB", "Alice 私有库" not in names, str(names))
    check("Carol 列表含 global KB", "全局公共库" in names)

    r = c.get(f"{API}/kb", headers=hB)
    names_b = [k["name"] for k in r.json()]
    check("Bob 列表含 workspace KB", "团队共享库" in names_b, str(names_b))
    check("Bob 列表不含 Alice private KB（private 只对创建者）",
          "Alice 私有库" not in names_b, str(names_b))
    check("Bob 列表不含 Carol 的私有库",
          all(k["visibility"] != "private" or k["created_by"] == b_id
              for k in r.json()), str(names_b))

    # ⚠️ 越权回归：不在 wsA 的用户**不能**看到 wsA 的 workspace 可见性库。
    # 这个曾经写成 `visibility.in_(("workspace","global"))` 导致全员可见。
    r = c.get(f"{API}/kb", headers=hC)
    vis_by_name = {k["name"]: k["visibility"] for k in r.json()}
    check("Carol（不在 wsA）看不到 wsA 的 workspace 库",
          "团队共享库" not in vis_by_name, str(vis_by_name))
    check("Carol 只看到 global 库",
          all(v == "global" for v in vis_by_name.values()), str(vis_by_name))

    section("7. 软删除（D26 / D42）")
    r = c.delete(f"{API}/kb/{KB_WS}", headers=hB)
    check("member 删 workspace KB → 204", r.status_code == 204, f"HTTP {r.status_code}")

    r = c.get(f"{API}/kb/{KB_WS}", headers=hB)
    check("删后读不到 → 403", r.status_code == 403, f"HTTP {r.status_code}")

    r = c.get(f"{API}/kb", headers=hB)
    check("列表里也不见了", "团队共享库" not in [k["name"] for k in r.json()])

    section("8. API Key 管理")
    hA = {"Authorization": f"Bearer {K_A}"}
    r = c.post(f"{API}/auth/keys", headers=hA, json={"name": "Cursor"})
    check("新建 key → 201 且返回明文", r.status_code == 201
          and r.json()["api_key"].startswith("sk-pkh_"),
          r.json()["api_key"][:20] + "...")
    KEY2 = r.json()["api_key"]
    KEY2_ID = r.json()["key"]["id"]

    r = c.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {KEY2}"})
    check("新 key 可用", r.status_code == 200)

    r = c.get(f"{API}/auth/keys", headers=hA)
    check("列 key 不含明文", r.status_code == 200
          and all("api_key" not in k for k in r.json()),
          f"{len(r.json())} 把")

    r = c.delete(f"{API}/auth/keys/{KEY2_ID}", headers=hA)
    check("吊销 key → 204", r.status_code == 204, f"HTTP {r.status_code}")

    r = c.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {KEY2}"})
    check("吊销后立即失效 → 401", r.status_code == 401)

    r = c.get(f"{API}/auth/keys", headers={"Authorization": f"Bearer {K_B}"})
    other_ids = [k["id"] for k in r.json()]
    check("看不到别人的 key", KEY2_ID not in other_ids)

    section("9. 登录换 key")
    r = c.post(f"{API}/auth/login",
               json={"email": "alice@t.local", "password": "alice-pass-123"})
    check("登录 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    NEW_KEY = r.json()["api_key"]
    check("  返回新 key", NEW_KEY.startswith("sk-pkh_"))
    check("  与原 key 不同", NEW_KEY != K_A)
    r = c.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {NEW_KEY}"})
    check("  新 key 可用", r.status_code == 200)

    r = c.post(f"{API}/auth/login",
               json={"email": "alice@t.local", "password": "wrong-pass-x"})
    check("密码错误 → 400", r.status_code == 400,
          r.json()["error"]["message"][:20])

    r = c.post(f"{API}/auth/login",
               json={"email": "nobody@t.local", "password": "whatever-1234"})
    check("用户不存在 → 与密码错同码同消息（不可枚举账号）",
          r.status_code == 400
          and r.json()["error"]["message"] == "账号或密码不正确")

    section("10. 健康检查")
    r = c.get("/health")
    check("免鉴权 /health → 200", r.status_code == 200)
    r = c.get(f"{API}/health", headers=hA)
    check("鉴权 /health 带模型状态", r.status_code == 200
          and "text_model" in r.json().get("models", {}),
          str(list(r.json().get("models", {}).keys())))

    # ================================================================
    section("11. 邀请码（D4 不接邮件服务 → 自服务加入）")
    # ------------------------------------------------------------
    HA = {"Authorization": f"Bearer {K_A}"}
    HB = {"Authorization": f"Bearer {K_B}"}
    ws = c.get(f"{API}/workspaces", headers=HA).json()[0]["id"]
    r = c.post(f"{API}/workspaces/{ws}/invites", headers=HA,
               json={"ttl_days": 7, "max_uses": 1})
    check("owner 生成邀请码 → 201", r.status_code == 201, f"HTTP {r.status_code}")
    inv = r.json() if r.status_code == 201 else {}
    code = inv.get("code") or ""
    meta = inv.get("invite") or {}
    check("  返回明文（只此一次，D64 同一纪律）", len(code) == 8, f"{len(code)} 位")
    check("  🔑 不含易混字符（手抄错一位最常见）",
          not (set(code) & set("01OIL")), code)
    check("  列表里**只有前缀**（4 位），没有明文",
          len(meta.get("code_prefix", "")) == 4
          and code not in json.dumps(meta, ensure_ascii=False),
          meta.get("code_prefix"))
    check("  状态与到期都给了",
          meta.get("state") == "可用" and meta.get("expiry_text") == "7 天后到期",
          f"{meta.get('state')} / {meta.get('expiry_text')}")

    lst = c.get(f"{API}/workspaces/{ws}/invites", headers=HA)
    check("列表端点可用，且**不含明文**",
          lst.status_code == 200 and len(lst.json() or []) >= 1
          and code not in lst.text, f"{len(lst.json() or [])} 条")

    # ⚠️ **第一次兑换必须用一个"还没加入这个工作区"的人**。
    #   我第一版用 Bob —— 而 §4「成员管理」早就把 Bob 加进 Alice 的工作区了，
    #   于是兑换走的是 `already_member` 分支、**根本没消耗邀请码**，
    #   后面"别人用同一码应该失败"就变成了 200。
    #   症状是"限次好像没生效"，而实现是对的 —— 断言的前提错了。
    #   （顺带：这也证明 `already_member` 分支确实在走。）
    HC = {"Authorization": f"Bearer {K_C}"}

    # ---- 兑换：乱格式（小写/空格/连字符）也要认 ----
    r = c.post(f"{API}/invites/redeem", headers=HC,
               json={"code": f" {code[:4].lower()}-{code[4:]} "})
    body1 = r.json() if r.status_code == 200 else {}
    check("兑换（乱格式：小写+空格+连字符）→ 200", r.status_code == 200,
          f"HTTP {r.status_code} {r.text[:70]}")
    check("  真的加入了（already_member=False，码被消耗）",
          body1.get("already_member") is False
          and body1.get("workspace_id") == ws,
          body1.get("workspace_name"))

    # ---- 重复兑换：给"你已经是成员"，**不是**"码无效" ----
    r = c.post(f"{API}/invites/redeem", headers=HC, json={"code": code})
    body2 = r.json() if r.status_code == 200 else {}
    check("🔑 重复兑换 → 200 + already_member（不是「码无效」）",
          r.status_code == 200 and body2.get("already_member") is True,
          f"HTTP {r.status_code} {body2.get('message')}")

    # ---- 第三个人用同一个码 → 统一失败文案（限次生效）----
    # ⚠️ 邮箱带时间戳：`_wipe_test_data()` 只删 alice/bob/carol 三个固定邮箱，
    #   用固定的新邮箱会在第二次跑时拿到 **409**（已注册）→
    #   `r.json()["api_key"]` 抛 KeyError，而报错完全指不到"清理列表漏了它"。
    _third = f"carol-3rd-{int(time.time())}@t.local"
    r = c.post(f"{API}/auth/register",
               json={"email": _third, "password": "carol-pass-12",
                     "display_name": "第三人"})
    H3 = {"Authorization": f"Bearer {r.json()['api_key']}"}
    r = c.post(f"{API}/invites/redeem", headers=H3, json={"code": code})
    msg = (r.json().get("error", {}).get("message", "")
           if r.status_code != 200 else "")
    check("🔑 别人用同一码 → 400（限次生效）", r.status_code == 400,
          f"HTTP {r.status_code}")
    check("  🔴 失败文案**统一**（不区分「用过/过期/撤销」）",
          "邀请码无效" in msg, msg[:90])

    # ---- 越权：非成员不能生成/列表 ----
    r = c.post(f"{API}/workspaces/{ws}/invites", headers=HB, json={})
    check("非成员生成邀请码 → 403（fail-closed）", r.status_code == 403,
          f"HTTP {r.status_code}")
    r = c.get(f"{API}/workspaces/{ws}/invites", headers=HB)
    check("非成员列邀请码 → 403", r.status_code == 403, f"HTTP {r.status_code}")

    # ---- 撤销：撤销后不能兑换；重复撤销幂等 ----
    r = c.post(f"{API}/workspaces/{ws}/invites", headers=HA,
               json={"max_uses": 3, "ttl_days": 1})
    inv2 = r.json() if r.status_code == 201 else {}
    inv_id = (inv2.get("invite") or {}).get("id")
    code2 = inv2.get("code") or ""
    r = c.delete(f"{API}/workspaces/{ws}/invites/{inv_id}", headers=HA)
    check("撤销 → 204", r.status_code == 204, f"HTTP {r.status_code}")
    r = c.delete(f"{API}/workspaces/{ws}/invites/{inv_id}", headers=HA)
    check("  重复撤销幂等（仍 204，不是 4xx）", r.status_code == 204,
          f"HTTP {r.status_code}")
    r = c.post(f"{API}/invites/redeem", headers=H3, json={"code": code2})
    check("  🔑 撤销后不能兑换", r.status_code == 400, f"HTTP {r.status_code}")

    # ---- 参数边界 ----
    r = c.post(f"{API}/workspaces/{ws}/invites", headers=HA,
               json={"max_uses": 999})
    check("max_uses=999 → 422（不能把工作区变公开仓库）",
          r.status_code == 422, f"HTTP {r.status_code}")
    r = c.post(f"{API}/workspaces/{ws}/invites", headers=HA,
               json={"ttl_days": 0})
    check("ttl_days=0 → 422（必须有有效期）", r.status_code == 422,
          f"HTTP {r.status_code}")

    # ---- 成员列表里能看到兑换进来的人 ----
    mem = c.get(f"{API}/workspaces/{ws}/members", headers=HA).json()
    check("兑换者出现在成员列表里",
          any(m["email"].startswith("carol@") for m in mem),
          f"{len(mem)} 位成员")

    # ================================================================
    section("12. 运行时 PII 掩码（D121 · 默认关闭 + 只读报告）")
    # ------------------------------------------------------------
    # 掩码**不可逆**，默认必须关 —— 关掉才保得住"搜我自己的手机号"
    HA = {"Authorization": f"Bearer {K_A}"}
    HB = {"Authorization": f"Bearer {K_B}"}
    ws_pii = c.get(f"{API}/workspaces", headers=HA).json()[0]["id"]
    kb_off = c.post(f"{API}/kb", headers=HA,
                    json={"workspace_id": ws_pii, "name": "pii-关"}).json()
    check("🔑 新建库的掩码开关**默认关闭**",
          kb_off.get("pii_mask") is False, kb_off.get("pii_mask"))

    PII_TEXT = ("我的身份证是110101199001011234，手机13812345678，"
                "邮箱user@example.com，学号20230001003。今天天气不错。")

    def _upload(kb_id: int, name: str, mask: bool) -> int:
        c.patch(f"{API}/kb/{kb_id}", headers=HA, json={"pii_mask": mask})
        up = c.post(f"{API}/kb/{kb_id}/documents", headers=HA,
                    files={"files": (name, PII_TEXT.encode(), "text/plain")})
        doc_id = up.json()["documents"][0]["id"]
        for _ in range(40):
            time.sleep(1.2)
            rows = c.get(f"{API}/kb/{kb_id}/documents", headers=HA).json()
            row = next((x for x in rows if x["id"] == doc_id), None)
            if row and row["status"] in ("ready", "failed"):
                return doc_id
        return doc_id

    def _wait_doc(kb_id: int, doc_id: int) -> dict:
        for _ in range(40):
            time.sleep(1.2)
            rows = c.get(f"{API}/kb/{kb_id}/documents", headers=HA).json()
            row = next((x for x in rows if x["id"] == doc_id), None)
            if row and row["status"] in ("ready", "failed"):
                return row
        return {}

    def _chunk_text(doc_id: int) -> str:
        r = c.get(f"{API}/documents/{doc_id}/chunks", headers=HA)
        return "".join(x["content_norm"] for x in r.json())

    kb_on = c.post(f"{API}/kb", headers=HA,
                   json={"workspace_id": ws_pii, "name": "pii-开"}).json()
    doc_off = _upload(kb_off["id"], "pii-off.txt", False)
    doc_on = _upload(kb_on["id"], "pii-on.txt", True)

    txt_off, txt_on = _chunk_text(doc_off), _chunk_text(doc_on)
    check("未开掩码：chunk 里保留原文手机号（否则搜不到自己的资料）",
          "13812345678" in txt_off, "缺原文手机号")
    check("🔑 开了掩码：chunk 里**没有**原文手机号",
          "13812345678" not in txt_on, "仍有原文手机号 → 掩码没生效")
    check("  开了掩码：chunk 里**没有**原文身份证",
          "110101199001011234" not in txt_on, "仍有原文身份证")
    check("  开了掩码：掩码串在（不是把内容删了）",
          "138****5678" in txt_on, "找不到掩码串")
    check("  🔑 无关内容不受影响（掩码只动命中段）",
          "今天天气不错" in txt_on, "正常文本被误伤")

    r = c.get(f"{API}/kb/{kb_on['id']}/pii", headers=HA)
    rep_on = r.json() if r.status_code == 200 else {}
    check("报告端点可用", r.status_code == 200, f"HTTP {r.status_code}")
    check("  masking=on", rep_on.get("masking") == "on", rep_on.get("masking"))
    check("  🔑 **原文**计数 ≥ 4（掩码前记下的）",
          rep_on.get("source_total", 0) >= 4, rep_on.get("source_total"))
    check("  🔑 **索引**计数 = 0（掩码真的生效了）",
          rep_on.get("indexed_total") == 0, rep_on.get("indexed_total"))
    check("  🔑 报告**不含原文片段**（否则报告本身成了泄露面）",
          "13812345678" not in r.text, "报告里出现了原文片段")
    check("  按类型计数都在",
          set(rep_on.get("source_by_type", {})) >= {"phone", "email", "id_card"},
          rep_on.get("source_by_type"))
    check("  文档明细列出了是哪几份",
          len(rep_on.get("docs", [])) >= 1,
          [(d["file_name"], d["total"]) for d in rep_on.get("docs", [])])

    r = c.get(f"{API}/kb/{kb_off['id']}/pii", headers=HA)
    rep_off = r.json() if r.status_code == 200 else {}
    check("未开掩码的库：索引里还剩 = 原文数",
          rep_off.get("indexed_total") == rep_off.get("source_total"),
          f"{rep_off.get('indexed_total')} / {rep_off.get('source_total')}")
    check("  masking=off", rep_off.get("masking") == "off", rep_off.get("masking"))

    again = c.get(f"{API}/kb/{kb_off['id']}/pii", headers=HA).json()
    check("🔑 只读（连读两次数字一致）",
          again.get("indexed_total") == rep_off.get("indexed_total"),
          f"{rep_off.get('indexed_total')} → {again.get('indexed_total')}")

    r = c.get(f"{API}/kb/{kb_on['id']}/pii", headers=HB)
    check("越权读报告 → 403（fail-closed）", r.status_code == 403,
          f"HTTP {r.status_code}")
    r = c.patch(f"{API}/kb/{kb_on['id']}", headers=HB, json={"pii_mask": False})
    check("越权改掩码开关 → 403", r.status_code == 403, f"HTTP {r.status_code}")

    # ---- 开开关**不回溯**已有 chunk，reparse 才生效（D121）----
    kb_late = c.post(f"{API}/kb", headers=HA,
                     json={"workspace_id": ws_pii, "name": "pii-半"}).json()
    late_doc = _upload(kb_late["id"], "pii-late.txt", False)
    c.patch(f"{API}/kb/{kb_late['id']}", headers=HA, json={"pii_mask": True})
    rep_late = c.get(f"{API}/kb/{kb_late['id']}/pii", headers=HA).json()
    check("开开关**不回溯**：索引里还剩 = 原文数",
          rep_late.get("indexed_total") == rep_late.get("source_total"),
          f"{rep_late.get('indexed_total')} / {rep_late.get('source_total')}")
    c.post(f"{API}/documents/{late_doc}/reparse", headers=HA)
    _wait_doc(kb_late["id"], late_doc)
    rep_after = c.get(f"{API}/kb/{kb_late['id']}/pii", headers=HA).json()
    check("  reparse 后索引计数归 0（回填路径可用）",
          rep_after.get("indexed_total") == 0, rep_after.get("indexed_total"))
    check("  🔑 但原文计数不变（掩的是索引，不是原件）",
          rep_after.get("source_total") == rep_late.get("source_total"),
          f"{rep_late.get('source_total')} → {rep_after.get('source_total')}")

# ---- 回收站：库 / 工作区（D129 + D130）----
    #
    # 🔑 这一段专门验**越权边界与顺序**，因为回收站是唯一一处
    #   "把已软删的实体暴露给列表"的地方：
    #   ① 别人的软删库不在我的回收站里（列表 = 我能恢复的那些）
    #   ② 恢复别人的软删库 → 与"不存在"同码（fail-closed，§0.4）
    #   ③ 工作区软删 → 它的库**从回收站消失**（先恢复工作区，D130）
    #   ④ 恢复工作区 → 那个库**又出现**（顺序可逆）
    #   ⑤ 恢复工作区回收站**只列 owner** 的（成员列不到）
    section("回收站：库 / 工作区（D129 · D130 的越权与顺序）")
    ws_tr = c.post(f"{API}/workspaces", headers=HA, json={"name": "回收站专用"}).json()
    kb_mine = c.post(f"{API}/kb", headers=HA, json={
        "workspace_id": ws_tr["id"], "name": "待删-我的",
        "visibility": "private", "source_kind": "file"}).json()
    c.post(f"{API}/members", headers=HA, json={
        "workspace_id": ws_tr["id"], "email": "bob@t.local", "role": "member"})

    # Bob 建一个 private 库并删掉 → 它不该出现在 Alice 的回收站里
    ws_bob = c.post(f"{API}/workspaces", headers=HB, json={"name": "Bob 的"}).json()
    kb_bob = c.post(f"{API}/kb", headers=HB, json={
        "workspace_id": ws_bob["id"], "name": "待删-Bob的",
        "visibility": "private", "source_kind": "file"}).json()
    check("Bob 删自己的库 → 204", c.delete(
        f"{API}/kb/{kb_bob['id']}", headers=HB).status_code == 204, "")

    c.delete(f"{API}/kb/{kb_mine['id']}", headers=HA)
    r = c.get(f"{API}/kb/trash", headers=HA)
    names = [x["name"] for x in r.json()] if r.status_code == 200 else []
    check("GET /kb/trash 可用", r.status_code == 200, f"HTTP {r.status_code}")
    check("  🔑 我删的库在列表里（看得见 = 功能成立的一半）",
          "待删-我的" in names, str(names))
    check("  🔑 别人的软删库**不在**我的列表里（列表 = 我能恢复的）",
          "待删-Bob的" not in names, str(names))
    check("  🔑 条目带 deleted_at（前端要算还有几天）",
          all(x.get("deleted_at") for x in r.json()),
          str([(x["name"], x.get("deleted_at")) for x in r.json()])[:120])

    r = c.post(f"{API}/kb/{kb_bob['id']}/restore", headers=HA)
    check("🔑 恢复别人的软删库 → 与'不存在'同码（fail-closed）",
          r.status_code in (403, 404), f"HTTP {r.status_code}")
    # ⚠️ 判据用 **Bob 自己的回收站**，不用 `GET /kb/{id}` ——
    #   软删的库本来就取不到（403），拿它当判据会把"仍然软删"误判成"没恢复"
    #   （第一版就是这么写错的：软删库里 GET 永远是 403，§5.123）。
    b_names = [x["name"] for x in c.get(f"{API}/kb/trash", headers=HB).json()]
    check("  越权恢复没有真的生效（它仍在 Bob 的回收站里）",
          "待删-Bob的" in b_names, str(b_names))

    # 🔴 D130：工作区软删 → 它的库从回收站消失；恢复工作区 → 又出现
    c.delete(f"{API}/workspaces/{ws_tr['id']}", headers=HA)
    names_ws = [x["name"] for x in c.get(f"{API}/kb/trash", headers=HA).json()]
    check("🔑 D130 工作区软删后它的库从回收站消失（先恢复工作区）",
          "待删-我的" not in names_ws, str(names_ws))
    r_gone = c.get(f"{API}/kb/{kb_mine['id']}", headers=HA)
    check("  🔴 D130 它也不再可取（GET /kb/{id} → 403，与'不存在'同码）",
          r_gone.status_code == 403, f"HTTP {r_gone.status_code}")
    r = c.post(f"{API}/kb/{kb_mine['id']}/restore", headers=HA)
    check("  工作区还软删时按 id 恢复也失败（与列表同源）",
          r.status_code in (403, 404), f"HTTP {r.status_code}")

    r = c.post(f"{API}/workspaces/{ws_tr['id']}/restore", headers=HA)
    check("恢复工作区 → 200", r.status_code == 200, f"HTTP {r.status_code}")
    names_back = [x["name"] for x in c.get(f"{API}/kb/trash", headers=HA).json()]
    check("🔑 恢复工作区后那个库**又出现**（顺序可逆）",
          "待删-我的" in names_back, str(names_back))

    # 工作区回收站只列 owner
    # ⚠️ 上面刚把工作区**恢复**了，所以这里要**再删一次** ——
    #   否则回收站当然是空的，而断言会红成"端点坏了"（第一版就这样：
    #   判据指着一个中间状态，指错了性质，§5.123）。
    c.delete(f"{API}/workspaces/{ws_tr['id']}", headers=HA)
    r = c.get(f"{API}/workspaces/trash", headers=HA)
    ws_names = [x["name"] for x in r.json()] if r.status_code == 200 else []
    check("GET /workspaces/trash 可用且含我删的",
          r.status_code == 200 and "回收站专用" in ws_names, str(ws_names))
    r = c.get(f"{API}/workspaces/trash", headers=HB)
    check("  🔑 成员看不到 owner 删掉的工作区（只列 owner 的）",
          "回收站专用" not in [x["name"] for x in r.json()],
          str([x["name"] for x in r.json()]))
    r = c.post(f"{API}/workspaces/{ws_tr['id']}/restore", headers=HB)
    check("  🔑 成员恢复它 → 403（fail-closed）", r.status_code == 403,
          f"HTTP {r.status_code}")

    # 收尾：把这一段造出来的东西恢复掉，别留给后面的脚本
    c.post(f"{API}/kb/{kb_mine['id']}/restore", headers=HA)
    c.post(f"{API}/kb/{kb_bob['id']}/restore", headers=HB)
    c.post(f"{API}/workspaces/{ws_bob['id']}/restore", headers=HB)

    section("18. 工作区用量统计（D132 · M9「磁盘用量按工作区统计」）")
    # 🔑 M9 那行写的是「按工作区统计 ✅」，而 D132 之前**只有单库统计**
    #   （`GET /kb/{id}`）—— 这一节钉的就是它现在真的存在、且口径一致。
    ws_st = c.post(f"{API}/workspaces", headers=HA, json={"name": "统计专用"}).json()
    kb_st = c.post(f"{API}/kb", headers=HA, json={
        "workspace_id": ws_st["id"], "name": "统计用库",
        "visibility": "private", "source_kind": "file"}).json()
    up = c.post(f"{API}/kb/{kb_st['id']}/documents", headers=HA,
                files={"files": ("统计.txt", "统计口径要能对上单库统计".encode(),
                                 "text/plain")})
    check("上传一份文档 → 202", up.status_code == 202, f"HTTP {up.status_code}")
    doc_st = up.json()["documents"][0]["id"]
    for _ in range(40):
        time.sleep(1.0)
        row = next((d for d in c.get(f"{API}/kb/{kb_st['id']}/documents", headers=HA).json()
                    if d["id"] == doc_st), {})
        if row.get("status") in ("ready", "failed"):
            break

    r = c.get(f"{API}/workspaces/{ws_st['id']}/stats", headers=HA)
    check("GET /workspaces/{id}/stats → 200", r.status_code == 200,
          f"HTTP {r.status_code} {r.text[:80]}")
    st = r.json() if r.status_code == 200 else {}
    check("  字段都在（含**两个**字节数：原始 vs 占盘）",
          {"kb_count", "doc_count", "chunk_count",
           "sum_doc_size_bytes", "blob_disk_bytes"} <= set(st),
          str(sorted(st)))
    kb_detail = c.get(f"{API}/kb/{kb_st['id']}", headers=HA).json()
    check("🔑 聚合 == 单库统计（口径漂移会红）",
          st.get("doc_count") == kb_detail.get("doc_count")
          and st.get("chunk_count") == kb_detail.get("chunk_count")
          and st.get("sum_doc_size_bytes") == kb_detail.get("size_bytes"),
          f"聚合 {st.get('doc_count')}/{st.get('chunk_count')}"
          f"/{st.get('sum_doc_size_bytes')} vs 单库 "
          f"{kb_detail.get('doc_count')}/{kb_detail.get('chunk_count')}"
          f"/{kb_detail.get('size_bytes')}")

    r = c.get(f"{API}/workspaces/{ws_st['id']}/stats", headers=HB)
    check("🔑 非成员读统计 → 403（与「不存在」同码）", r.status_code == 403,
          f"HTTP {r.status_code}")
    # ⚠️ 这条端点是 **owner 口径**的磁盘账：它**包含**工作区里所有库
    #   （包括别人的 private）—— 看不见的文档仍然占盘。
    #   把它按可见性"修"好会让"删哪个库能腾出空间"算错（§5.131）。

    # 软删的工作区 → 403（D130 的顺序：先恢复工作区）
    c.delete(f"{API}/workspaces/{ws_st['id']}", headers=HA)
    r = c.get(f"{API}/workspaces/{ws_st['id']}/stats", headers=HA)
    check("  软删后 → 403", r.status_code == 403, f"HTTP {r.status_code}")
    c.post(f"{API}/workspaces/{ws_st['id']}/restore", headers=HA)

    # 收尾：删掉这一节造出来的库
    c.delete(f"{API}/kb/{kb_st['id']}", headers=HA)

    REGISTER_LIMIT_PROBE = 20     # 与 services/api/rate_limit.py::REGISTER_LIMIT 一致
    LOGIN_FAIL_PROBE = 10         # 与 ::LOGIN_FAIL_LIMIT 一致
    LOGIN_ACCOUNT_PROBE = 20      # 与 ::LOGIN_FAIL_ACCOUNT_LIMIT 一致
    section("19. 注册限流（D135 · 它不需要 API Key 却对公网可达）")
    # 🔑 与 D138 的开关**互斥**：自助注册一旦对公网关闭，限流那条路径
    #   对公网就**不可达**了（开关在限流**之前**，403 一次都不记桶）。
    #   所以这里按"注册对公网开着吗"分支，别把"触发不了"报成失败。
    rl_probe = {"cf-connecting-ip": "203.0.113.240"}
    first = c.post(f"{API}/auth/register", headers=rl_probe, json={
        "email": f"ratelimit-{int(time.time())}@t.local",
        "password": "rl-pass-12345", "display_name": "限流探针"})
    if first.status_code == 403 and \
            first.json().get("error", {}).get("code") == "REGISTRATION_CLOSED":
        print(f"⚠️ 注册已对公网关闭（D138）⇒ 限流那条路径对公网不可达，"
              f"由 §20 的 403 覆盖；限流本身由 tests/test_register_rate_limit.py 钉住")
        check("🔑 关着的时候第一个请求就是 403（不建号、不记桶）",
              first.status_code == 403, f"HTTP {first.status_code}")
    else:
        # 🔑 用**同一个邮箱**连打 N+1 次：限流排在业务判断之前，所以
        #   第 21 次拿到的是 429 而不是"该邮箱已注册"—— 这同时验证了
        #   "限流先于资源探测"这条设计。而库里只会多出 **1** 个用户。
        rl_mail = f"ratelimit-{int(time.time())}@t.local"
        seen_429 = None
        for i in range(REGISTER_LIMIT_PROBE + 2):
            rr = c.post(f"{API}/auth/register", headers=rl_probe,
                        json={"email": rl_mail, "password": "rl-pass-12345",
                              "display_name": "限流探针"})
            if rr.status_code == 429:
                seen_429 = rr
                break
        check("🔑 连续注册触发 429", seen_429 is not None,
              f"试了 {REGISTER_LIMIT_PROBE + 2} 次没触发")
        if seen_429 is not None:
            check("  错误码是 RATE_LIMITED",
                  seen_429.json()["error"]["code"] == "RATE_LIMITED",
                  str(seen_429.json()["error"])[:90])
            ra = seen_429.headers.get("retry-after")
            check("  🔑 带 Retry-After 头（客户端按标准取它）",
                  ra is not None and ra.isdigit() and int(ra) > 0, str(ra))
            check("  🔑 超限时不泄露「邮箱已注册」（限流先于资源探测）",
                  seen_429.json()["error"]["code"] != "INVALID_REQUEST",
                  seen_429.json()["error"]["code"])
    # 本机来源（无 CF 头）**不受影响** —— 否则验收脚本每轮都会把自己限死
    rl_local = c.post(f"{API}/auth/register", json={
        "email": f"ratelimit-local-{int(time.time())}@t.local",
        "password": "rl-pass-12345", "display_name": "本机"})
    check("  本机来源不受影响（单独的宽松桶）", rl_local.status_code == 201,
          f"HTTP {rl_local.status_code}")

    section("20. 对外加固（D138：登录限流 / 自助注册开关 / 文档开关）")
    # ⚠️ 本节带 `cf-connecting-ip` ⇒ 桶键是 `ip:…`，用来模拟**公网**来源。
    #   公网路径的判定与本机不同（见 `services/api/routers/auth.py` 的注册开关）。
    pub = {"cf-connecting-ip": "203.0.113.211"}

    # ---- 20.1 登录失败限流：唯一通往数据的入口 ----
    # 🔑 打**不存在**的邮箱：不需要真账号，也不会碰用户的密码；
    #   判据是"连续失败会 429"，与账号是否存在无关（端点两种情况都返回 400）。
    lf_codes = []
    got = None
    for i in range(LOGIN_FAIL_PROBE + 2):
        rr = c.post(f"{API}/auth/login", headers=pub,
                    json={"email": "no-such-user@t.local",
                          "password": f"wrong-{i}"})
        lf_codes.append(rr.status_code)
        if rr.status_code == 429:
            got = rr
            break
    check("🔑 连续登录失败触发 429", got is not None,
          f"试了 {len(lf_codes)} 次没触发：{lf_codes[:5]}…")
    if got is not None:
        check("  错误码是 RATE_LIMITED",
              got.json()["error"]["code"] == "RATE_LIMITED",
              str(got.json()["error"])[:90])
        ra = got.headers.get("retry-after")
        check("  🔑 带 Retry-After 头", ra is not None and ra.isdigit(), str(ra))
        msg = got.json()["error"]["message"].lower()
        check("  🔑 文案不泄露是哪个桶（IP 还是账号）",
              "ip" not in msg and "account" not in msg, got.json()["error"]["message"])
        check("  🔑 超限时**不**说密码对不对（不帮攻击者省哈希）",
              "密码" not in got.json()["error"]["message"],
              got.json()["error"]["message"])

    # ---- 20.2 账号桶：换 IP 也挡得住 ----
    # ⚠️ 用**同一个邮箱** + 每次换 IP ⇒ IP 桶（每 IP 10 次）不会命中，
    #   只有账号桶（同一账号 20 次）会 —— 这正是"保护某个具体用户"的性质。
    acct_codes = []
    acct_429 = None
    for i in range(LOGIN_ACCOUNT_PROBE + 2):
        rr = c.post(f"{API}/auth/login",
                    headers={"cf-connecting-ip": f"198.51.100.{i + 1}"},
                    json={"email": "victim@t.local", "password": "wrong"})
        acct_codes.append(rr.status_code)
        if rr.status_code == 429:
            acct_429 = rr
            break
    check("🔑 同一账号换着 IP 打也会被拦（账号桶）", acct_429 is not None,
          f"试了 {len(acct_codes)} 次没触发")

    # ---- 20.3 自助注册开关：对公网关掉、对本机留后门 ----
    # ⚠️ 开关是**部署选择**（`.env` 的 `ALLOW_REGISTER`），所以脚本读自己的
    #   settings 来判断该期待什么 —— 别把"开着"也报成失败。
    _switch_on = _allow_register_setting()
    rr = c.post(f"{API}/auth/register", headers=pub, json={
        "email": f"closed-{int(time.time())}@t.local",
        "password": "pw12345678", "display_name": "应当被拒"})
    if _switch_on:
        check("  ALLOW_REGISTER 开着 ⇒ 公网可注册（本节不判失败）", rr.status_code in (201, 429),
              f"HTTP {rr.status_code}")
    else:
        check("🔑 ALLOW_REGISTER=false ⇒ 公网自助注册 → 403",
              rr.status_code == 403, f"HTTP {rr.status_code}")
        check("  错误码是 REGISTRATION_CLOSED",
              rr.json()["error"]["code"] == "REGISTRATION_CLOSED",
              str(rr.json().get("error"))[:90])
    # 🔑 本机仍能注册（eval 与 e2e 全靠它；关掉的话基线自己把自己锁死）
    rr = c.post(f"{API}/auth/register", json={
        "email": f"local-still-open-{int(time.time())}@t.local",
        "password": "pw12345678", "display_name": "本机"})
    check("  🔑 本机来源仍可注册（关掉开关不误伤基线）",
          rr.status_code == 201, f"HTTP {rr.status_code}")

    # ---- 20.4 文档开关 ----
    rr = c.get(f"{BASE}/docs")
    ro = c.get(f"{BASE}/openapi.json")
    if _expose_docs_setting():
        check("EXPOSE_DOCS 开着 ⇒ /docs 与 /openapi.json 公开（不判失败）",
              rr.status_code == 200 and ro.status_code == 200,
              f"/docs {rr.status_code} · /openapi.json {ro.status_code}")
    else:
        check("🔑 EXPOSE_DOCS=false ⇒ /docs 与 /openapi.json 都不可达",
              rr.status_code == 404 and ro.status_code == 404,
              f"/docs {rr.status_code} · /openapi.json {ro.status_code}")
    # 🔑 无论开关如何，API 面本身不受影响
    check("  🔑 开关不影响真正的 API（无 Key 仍 401）",
          c.get(f"{API}/kb").status_code == 401)

    section("结果")
    if _fail:
        print(f"{FAIL} {len(_fail)} 项失败：")
        for f in _fail:
            print(f"     - {f}")
        return 1
    print(f"{PASS} 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
