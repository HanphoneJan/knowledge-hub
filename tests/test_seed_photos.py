"""🔌 D152：`deploy/seed_photos.py` 的**接线与理由**守卫。

## 为什么这个脚本需要守卫

它是**本机运维脚本**（与 `deploy/monitor.py`、`deploy/backup.sh` 同类），
不在 pytest 的覆盖范围内 —— 而它出错的代价很特别：

⚠️ 它写的是**不会被任何清场脚本删掉**的数据（域与前缀都刻意避开
`@t.local` / `1e-` / `1f-` / `1g-`）。所以一旦它的"清场规避"被改坏
（比如有人"顺手统一"成 `@t.local`），后果不是脚本报错，而是
**下一轮基线把这批数据删掉** ⇒ `kb_visual` 又变 0 个点 ⇒ 症状看起来像
"colSmol 又坏了"（§5.93/§5.94 那一族：0 点到底是坏了还是没数据？）。

## 守卫的三条判据（都是**性质**，不是某一行代码）

1. **规避用的域/前缀不能被改掉**（上面那个"下一轮基线删掉"的形状）
2. **`PKH_KEEPER_KEY` 必须来自环境变量**，不能改成"从库里查"——
   库里只有哈希，查不到；而"再建一把"会让"我有几把能用的 Key"变不确定
3. **幂等**：已有就绪文档时必须**跳过导入**（重复导入同内容是
   `same_content` 跳过，白等一轮 ~2 分钟的视觉编码）

⚠️ **本守卫的盲区**（写下来是为了下一个人知道边界）：
- 它只查**文本**，不执行脚本 ⇒ 真跑起来的行为（HTTP 调用、终态轮询）
  要靠 `--check` 与实跑验证（`MEMORY.md` 记着怎么跑）。
- 它**不**检查表结构假设（`embed_model` NOT NULL、`workspaces` 没有
  `visibility` 列）—— 那两条是我写脚本时踩出来的，见脚本注释。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "deploy" / "seed_photos.py"
FIXTURES = ROOT / "eval" / "_fixtures.py"


def _src() -> str:
    return SEED.read_text(encoding="utf-8")


# ============================================================
# 1. 清场规避（最要紧的一条）
# ============================================================


def test_keeper_email_avoids_every_cleanup_rule():
    """🔑 账号邮箱必须同时躲开 eval 与 e2e 的清场规则。

    | 谁清 | 清什么 |
    |---|---|
    | `eval/api_1b` | `%@t.local` |
    | `web/e2e/*` | `1e-` / `1f-` / `1g-` **前缀** |

    ⚠️ 判据是"**与这些模式都不匹配**"，不是"包含某个魔法串" ——
    写成后者的话，改个邮箱格式就红，而它其实还是安全的（或反之）。
    """
    # 🔑 账号现在来自**配置**（`services/common/config.py` 的 `keeper_email`，可被 .env 覆盖）——
    #   判据落在"**生效的那个值**"上，而不是某一行字面量：
    #   写死字面量的版本会随"值改成从配置读"而失效，而它要守的性质（避开清场）没变。
    from services.common.config import Settings

    email = Settings().keeper_email
    assert email, "keeper_email 配成了空值"

    assert not email.endswith("@t.local"), (
        f"{email} 落在 eval 的清场域里（`%@t.local`）—— "
        "下一轮基线会把这批照片连同 kb_visual 的点一起删掉"
    )
    for prefix in ("1e-", "1f-", "1g-"):
        assert prefix not in email, (
            f"{email} 带 e2e 的清场前缀 {prefix!r} —— 同样会被下一轮删掉"
        )


def test_the_script_says_why_it_avoids_those_rules():
    """🔑 理由必须写在脚本里（否则下一个人只会看到"奇怪的域名"）。

    ⚠️ 判据是"**提到了清理者是谁**"，不是"表格长什么样"——
    第一版锚在那张 markdown 表的一整行上，结果任何排版改动都会让它失效，
    而它要守的性质（说清为什么）一点没变（§5.114：判据指向性质）。
    """
    src = _src()
    # 说清"谁在清" + "所以避开什么"，两者都要出现
    for token, why in (("api_1b", "eval 会清"),
                       ("e2e", "e2e 会清"),
                       ("@t.local", "要避开的域"),
                       ("1e-", "要避开的前缀")):
        assert token in src, f"脚本没提到 {token}（{why}）—— 后人无从判断能否改域名"

    # 🔑 还要**明确警告过**"别改"：只在表格里列出规则不够 ——
    #   变异脚本删掉那句警告时，表格里的 `@t.local` / `1e-` 还在，
    #   上面那条照样绿（"仍然全绿"又一次：判据查的是"出现过"，不是"被警告过"）。
    #   ⇒ 判据落在 docstring 里那句**祈使句**上。
    doc = ast.get_docstring(ast.parse(src)) or ""
    warn_lines = [ln for ln in doc.splitlines()
                  if "⚠️" in ln and ("KEEPER_EMAIL" in ln or "邮箱" in ln)]
    assert warn_lines, (
        "docstring 里没有一句「⚠️ 别改邮箱/域名」的警告 —— "
        "下一个人只会看到一个奇怪的域名，不知道能不能改"
    )
    assert any(k in " ".join(warn_lines) for k in ("@t.local", "1e-")), (
        "那句警告里没点明要避开什么（只说'别改'等于没说）"
    )


# ============================================================
# 2. Key 只能来自环境变量
# ============================================================


def test_key_comes_from_the_environment_not_the_database():
    """🔑 明文 Key 取自 `PKH_KEEPER_KEY`，**不许**从库里查。

    ⚠️ 库里只有哈希（D64/D134），查不到明文；而"查不到就再建一把"会让
    "我手上有几把能用的 Key"变成不确定的事 —— 真正要解决的问题是
    "当初那把没记下来"，那应该由人处理，不是脚本悄悄绕过。
    """
    src = _src()
    assert "PKH_KEEPER_KEY" in src, (
        "Key 应该来自环境变量 `PKH_KEEPER_KEY`（`.env` 里有建库时写下的那把）"
    )
    # 🔑 判据是"**脚本自己不去拿明文**"，判 `generate_api_key` 这个**名字**
    #   而不是"怎么从环境变量取值" —— 第一版锚在
    #   `os.environ.get(...)` 那一行，于是"多了一个 fallback"这种变异照样全绿，
    #   而那正是要防的事（从库里捞 / 现造一把 = 明文 Key 的来源被悄悄改了）。
    tree = ast.parse(src)
    funcs = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    seed_src = ast.unparse(next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "seed"))
    assert "generate_api_key" not in seed_src, (
        "脚本自己在生成新 Key —— 幂等重跑会每次多一把，'我有几把 Key'变不确定"
    )
    assert not re.search(r"select\(\s*ApiKey", seed_src), (
        "脚本去库里查 Key 了 —— 库里只有哈希（D64/D134），查不到"
    )
    # 也不许从别处读明文（keyring / 文件 / 命令行）
    for sneak in ("keyring", "PKH_KEEPER_KEY_FILE", "input("):
        assert sneak not in seed_src, f"Key 的来源被改了（出现了 {sneak}）"


def test_missing_key_fails_loudly_with_a_recovery_hint():
    """🔑 缺 Key 时**明确报错退出**（不是"带着空 Key 走下去"）。

    ⚠️ 判据用 AST：守卫分支里必须有 `return 1`。第一版锚在
    `if not key:` 加文案的**具体几行**上，于是"把守卫改成 `if False`"
    这种变异照样全绿 —— 而那正是最该抓住的（守卫形同不存在）。
    """
    tree = ast.parse(_src())
    seed_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "seed")
    guards = [n for n in ast.walk(seed_fn)
              if isinstance(n, ast.If) and "key" in ast.unparse(n.test).lower()
              and not ast.unparse(n.test).startswith("key")]
    assert guards, "seed() 里找不到针对 key 的守卫分支"
    exits = [g for g in guards
             if any(isinstance(x, ast.Return) and x.value is not None
                    and ast.unparse(x.value) == "1" for x in ast.walk(g))]
    assert exits, (
        "Key 缺失的分支没有 `return 1` —— 脚本会带着空 Key 走下去，"
        "而症状是后面某个完全无关的 401"
    )
    # 报错文案要给出找回办法。
    # ⚠️ 判据落在**守卫分支里的字符串**上，不是全文出现 ".env" ——
    #   全文判据在变异脚本里被骗过：删掉那句文案后 docstring 里的 `.env` 还在。
    msg = " ".join(
        n.value for g in guards for n in ast.walk(g)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    assert ".env" in msg, (
        "Key 缺失的报错里没告诉人 Key 记在 `.env` —— "
        "只说「没有 Key」的话，人只会去检查代码而想不到翻配置"
    )
    assert "PKH_KEEPER_KEY" in msg, "报错没点名是哪个环境变量"


# ============================================================
# 3. 幂等
# ============================================================


def test_existing_ready_documents_mean_skip_the_import():
    """🔑 已有就绪文档时**跳过导入**。

    ⚠️ 重复导入同内容会拿到 `same_content` 跳过（D114/D148），
    所以"重复跑一遍"不会坏数据 —— 但仍会跑一遍 job 派发与终态结算，
    白等 ~2 分钟的视觉编码（每张 ~15s）。幂等不只是"不出错"，是"不白等"。
    """
    src = _src()
    # 判据是"先查状态、再决定要不要导"这个**顺序**，不是某个具体字符串
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "seed" in funcs, "找不到 seed()"
    body = ast.unparse(funcs["seed"])

    get_docs = body.find("/documents?page_size=100")
    post_import = body.find("/import/dir")
    assert 0 <= get_docs < post_import, (
        "必须**先**看库里有没有就绪文档，再决定要不要导入 —— "
        "顺序反了就会每次都白等一轮视觉编码"
    )
    assert "ready" in body, "没有判断就绪状态"
    assert "跳过导入" in body, "跳过时应该明说跳过了（而不是静默什么都不做）"


def test_all_failed_is_reported_not_treated_as_done():
    """🔑 库里全是 `failed` 时**报错**，不当成"已完成"也不直接重导。

    ⚠️ 直接重导是**无效动作**：同内容会 `same_content` 跳过 ⇒
    脚本会"成功"退出而数据一点没变 —— 那种"看起来成功"最难查（§5.142）。
    """
    src = _src()
    assert "failed" in src and "same_content" in src, (
        "要能识别「全 failed 且重导无效」这个状态并说清"
    )


# ============================================================
# 4. 接线：fixture 与脚本对得上
# ============================================================


def test_fixture_generator_exists_and_is_referenced():
    """🔑 图片由 `eval/_fixtures.py` 生成（可复现，不是"我手搓的数据"）。

    ⚠️ 两边都要判：只判"脚本里提到了这个名字"，那生成器被删了也照样绿；
    只判"生成器存在"，那脚本改用手搓路径也照样绿。
    """
    fx = FIXTURES.read_text(encoding="utf-8")
    assert "def make_photo_keeper_fixture(" in fx, "fixture 生成器不见了"
    tree = ast.parse(_src())
    seed_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "seed")
    called = {n.func.id for n in ast.walk(seed_fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "make_photo_keeper_fixture" in called, (
        "脚本没有真的**调用** fixture 生成器（提到名字 ≠ 用了它）"
    )


def test_fixture_draws_distinguishable_shapes_not_solid_colors():
    """🔑 fixture 必须画**形状**而不是纯色块。

    🔴 这是 fixture 存在的**全部理由**：colSmol 的向量是对画面内容的编码。
    纯色块的 N 张图向量几乎一样 ⇒ 即便视觉腿正常写入，
    "视觉召回能不能区分它们"这件事也**测不出来**（相似度全一样）。
    有了明确的形状差异，才能对照文本腿看两条腿的分工（D50 实测就靠它）。
    """
    fx = FIXTURES.read_text(encoding="utf-8")
    body = fx[fx.index("def make_photo_keeper_fixture("):]
    for shape in ("ellipse", "rectangle", "polygon"):
        assert shape in body, f"fixture 里没有 {shape}（纯色块测不出视觉召回）"
    assert "_KEEPER_SHAPES" in fx, "形状清单不见了（那是 fixture 的判据来源）"

    # 🔑 **清单里的每一项都要真的画出来，而且要够多**。
    #   ⚠️ 只查"文件里有 ellipse 这个词"会被两种变异骗过：
    #   ① 把清单里的 `circle` 改名成 `solid-red`（其它形状仍在画 ellipse）；
    #   ② 把清单项从 8 种砍到 2 种。两者都让"8 张图形状明显不同"不成立，
    #   而那正是这个 fixture 存在的**唯一理由**（D50 实测靠它区分召回）。
    tree = ast.parse(fx)
    shapes_node = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.Assign, ast.AnnAssign))
         and any(getattr(t, "id", "") == "_KEEPER_SHAPES"
                 for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))),
        None,
    )
    assert shapes_node is not None, (
        "_KEEPER_SHAPES 常量不见了"
        # ⚠️ 这条断言第一次写成只找 `ast.Assign`，而带注解的赋值是
        #   `ast.AnnAssign`（`X: tuple[...] = (...)`）⇒ 测试自己红着。
        #   红着的测试会让**每一条**变异都"通过"（vacuous pass），
        #   而变异脚本报一片 ✅ —— 那 15/15 是假的。§5.130 的姊妹坑。
    )
    names = [e.elts[0].value for e in shapes_node.value.elts
             if isinstance(e, ast.Tuple) and isinstance(e.elts[0], ast.Constant)]
    assert len(names) >= 6, f"形状只有 {len(names)} 种（{names}）—— 区分度不够"
    for n_ in names:
        assert f'name == "{n_}"' in body, (
            f"清单里的 {n_!r} 没有对应的绘制分支 —— "
            "它会落进 else 分支画成同一个东西"
        )


def test_check_mode_is_read_only():
    """🔑 `--check` 只读（体检不该改任何东西）。"""
    tree = ast.parse(_src())
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "check" in funcs
    body = ast.unparse(funcs["check"])
    for danger in ("POST", "PUT", "PATCH", "DELETE"):
        assert danger not in body, f"check() 里出现了 {danger} —— 体检必须是只读"
    # 而 seed 才允许写
    assert "import/dir" in ast.unparse(funcs["seed"])
