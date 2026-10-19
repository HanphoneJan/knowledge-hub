#!/usr/bin/env bash
# 不需要任何外部服务与模型的**门禁**（CI 与本地跑的是同一条命令）。
#
# ## 为什么单独有这么一个脚本
#
# 全量基线（`pytest` 1251 项 + 三套 eval）需要：真实 Postgres(pgvector)、Qdrant、
# 以及本地缓存好的模型（`bge-small` 约 100MB，视觉腿还要 colSmol 2.65GB）。
# 那套东西**不适合放进托管 CI**：
#   - 模型要么进 CI 缓存、要么每次下载几百 MB，而 `HF_HUB_OFFLINE=1` 的离线纪律
#     又是这个项目的一条硬约束（§5.167）；
#   - 视觉腿在 CI 里必然 `skipped`，于是"跑过了"反而是假的绿灯。
# ⇒ 所以 CI 只跑**不需要它们**的那部分（契约 + 纯逻辑单测 + 前端构建与单测），
#   全量基线仍在本机跑（`MEMORY.md` §0/§3）。这条分工写在 README 里，别悄悄扩大 CI 的范围。
#
# ## 用法
#
# ```bash
# bash deploy/ci.sh              # 全部
# bash deploy/ci.sh web          # 只跑前端
# bash deploy/ci.sh py           # 只跑后端免基础设施那部分
# ```
#
# ⚠️ 本脚本**不碰数据库、不写任何数据**（除前端 `dist/` 构建产物）。
set -euo pipefail
cd "$(dirname "$0")/.."

#: 判定为"**不需要 DB / 模型 / 一个在跑的服务**"的测试文件。
#:
#: ⚠️ 加文件前**三个维度都要验**（前两个已经各踩过一次）：
#:   ① 数据库不可达：把 `DATABASE_URL` 指向一个不存在的库，仍应全绿；
#:   ② 服务不可达：把 `PKH_TEST_BASE` 指向一个没人听的端口、或确认该文件不发真 HTTP
#:      —— `httpx.Client(base_url="http://127.0.0.1:8010")` 那种"本机正好起着 API"
#:      的写法在这里会红，而它会连坐同模块的纯单测（TEST_BASE 一族就是这么被抓的）。
#:   ③ **locale**（前端那一半）：CI 是 `LANG=C`，本机常是 zh_CN ⇒ 任何
#:      `localeCompare` / `toLocaleString` 的**排序或格式**断言都可能只在本机绿。
#:      验法：`LANG=C LC_ALL=C npm test`。
PURE_TESTS=(
  # 文档与契约（纯解析/守卫）
  tests/test_docs_pointers.py
  tests/test_contract_parser.py
  # 真实信息不许进已跟踪文件（本地 deny-list 存在时全扫；结构检查永远生效）
  tests/test_no_real_data.py
  # 切分策略与解析（纯函数）
  tests/test_strategies.py
  tests/test_parser_registry.py
  tests/test_doc_processing.py
  tests/test_locations.py
  tests/test_office_locations.py
  # PDF 页眉/页脚清洗（纯函数，D201）
  tests/test_pdf_header_footer.py
  # 隐私与检索几何（纯规则/纯函数）
  tests/test_owner_classifier.py
  tests/test_pii.py
  tests/test_qdrant_filter_scope.py
  tests/test_fusion.py
  tests/test_rerank.py
  # 检索质量指标（纯算术，D194 评测回放的度量层）
  tests/test_retrieval_metrics.py
  # 查询改写（纯函数 + 失败兜底，D196）
  tests/test_rewrite.py
  # 证据质量判定（拒答信号，D197）
  tests/test_quality.py
  # 证据组装顺序（上下文工程，D198）
  tests/test_ask_evidence_order.py
  # 外部生成器失败 → 降级抽取式，不 500（D205）
  tests/test_ask_llm_degrade.py
  # 元数据（时间）过滤 + payload 字段（D199）
  tests/test_metadata_filter.py
  # 四条检索腿真并发：每条 DB 腿独立会话（纯逻辑，D207）
  tests/test_leg_concurrency.py
  # 展示与协议（纯逻辑 + 无 DB 的 TestClient）
  tests/test_progress_clamp.py
  tests/test_thumb_url.py
  tests/test_key_hash.py
  tests/test_visual_service.py
  # Embedding 一致性守卫（纯函数 + 源码级断言，D191）
  tests/test_index_models.py
  # 别名模式/蓝绿重建的纯判据（真 Qdrant 那部分在 test_alias_mode.py，会 skip，D193）
  tests/test_alias_pure.py
)

web() {
  echo "── web: typecheck / vitest / build ──"
  ( cd web && npm run typecheck && npm test && npm run build )
}

py() {
  echo "── py: 契约覆盖率 ──"
  uv run python eval/contract_check.py
  echo "── py: 免基础设施单测（$((${#PURE_TESTS[@]})) 个文件）──"
  uv run pytest "${PURE_TESTS[@]}" -q
}

case "${1:-all}" in
  web) web ;;
  py)  py ;;
  all) web; py ;;
  *)   echo "用法: bash deploy/ci.sh [web|py|all]" >&2; exit 2 ;;
esac

echo "✅ 门禁通过（全量基线需要本机基础设施与模型，见 README「验收」一节）"
