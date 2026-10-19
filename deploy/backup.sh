#!/usr/bin/env bash
# 🔑 备份（阶段 7 · D25）。
#
# ## 备份什么、为什么不备份什么
#
# | 内容 | 备份 | 理由 |
# |---|---|---|
# | Postgres（`pg_dump -Fc`） | ✅ | 唯一的权威数据源。**没有它就没有一切** |
# | `data/blobs/`（原始上传文件） | ✅ | 内容寻址的原件；`documents.blob_sha` 指向它，缺了就"文档还在、文件没了" |
# | Qdrant（向量库） | ❌ | **索引可重建**（D25）：备份它反而占空间，且可能与 Postgres 不一致 |
#
# 🔑 **Qdrant 不备份，但它的"期望值"必须记进 manifest**：每个 collection 的
# 点数（`kb_dense` / `kb_sparse` / `kb_visual`）。这样"重建后对不对得上"
# 有据可查 —— 否则"索引可重建"只是一句话，出问题时没人能判断重建结果对不对。
#
# ## ⚠️ 一次没验证过的备份等于没有备份
#
# 所以本脚本写完 manifest 后**立刻自检**（`--verify`），而
# `deploy/restore.sh` 能把备份恢复到**临时库**并逐表对账。
# 真正的"能恢复"是在 restore 那一侧证明的，不是这里。
#
# ## 用法
#
# ```bash
# bash deploy/backup.sh                     # 做一份（默认 data/backups/<时间戳>/）
# bash deploy/backup.sh --keep 7            # 只留最近 7 份（默认 14）
# bash deploy/backup.sh --verify DIR        # 只校验已有备份（不新建）
# bash deploy/backup.sh --list              # 列出已有备份（带大小与是否自检通过）
# ```
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
[ -f .env ] && { set -a; . ./.env; set +a; }

PG_USER="${POSTGRES_USER:-pkh}"
PG_DB="${POSTGRES_DB:-pkh}"
PG_PASS="${POSTGRES_PASSWORD:-pkh_local_dev}"
PG_CONTAINER="${PG_CONTAINER:-pkh-postgres}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
BLOB_DIR="${PKH_DATA_DIR:-data}/blobs"
BACKUP_ROOT="${PKH_BACKUP_DIR:-${PKH_DATA_DIR:-data}/backups}"
KEEP=14

# shellcheck disable=SC1090
source deploy/_backup_lib.sh

log()  { printf '\033[36m→\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------
# 参数
# ---------------------------------------------------------------
ACTION="make"
TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --keep)   KEEP="$2"; shift 2 ;;
    --out)    BACKUP_ROOT="$2"; TARGET="$2"; shift 2 ;;
    --verify) ACTION="verify"; TARGET="${2:?--verify 需要目录}"; shift 2 ;;
    --list)   ACTION="list"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) die "未知参数：$1（-h 看用法）" ;;
  esac
done

case "$ACTION" in
  list)
    if [ ! -d "$BACKUP_ROOT" ]; then echo "还没有备份（$BACKUP_ROOT 不存在）"; exit 0; fi
    printf '%-22s %10s  %s\n' 时间 大小 自检
    for d in "$BACKUP_ROOT"/*/; do
      [ -d "$d" ] || continue
      sz=$(du -sh "$d" 2>/dev/null | cut -f1)
      if bash deploy/_backup_lib.sh verify "$d" >/dev/null 2>&1; then ok="✓ 通过"; else ok="✗ 损坏"; fi
      printf '%-22s %10s  %s\n' "$(basename "$d")" "$sz" "$ok"
    done
    exit 0 ;;
  verify)
    bash deploy/_backup_lib.sh verify "$TARGET"
    exit $? ;;
esac

# ---------------------------------------------------------------
# 做备份
# ---------------------------------------------------------------
command -v docker >/dev/null || die "没有 docker"
command -v tar    >/dev/null || die "没有 tar"
docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER" \
  || die "容器 $PG_CONTAINER 不在跑（docker compose -f deploy/docker-compose.yml up -d）"
[ -d "$BLOB_DIR" ] || warn "$BLOB_DIR 不存在 —— 只备份数据库（没有原件，文档会显示不了）"

STAMP=$(date +%Y%m%d-%H%M%S)
DEST="${TARGET:-$BACKUP_ROOT/$STAMP}"
mkdir -p "$DEST"
log "备份目录：$DEST"

# ---- 1. Postgres ----
# ⚠️ `-Fc`（自定义格式）：已压缩、可用 `pg_restore` 按表选着恢复，
#    而纯 SQL（`-Fp`）在恢复前得整份解压才能看一眼内容。
log "dump Postgres（$PG_DB）…"
docker exec -e PGPASSWORD="$PG_PASS" "$PG_CONTAINER" \
  pg_dump -U "$PG_USER" -d "$PG_DB" -Fc -f /tmp/pkh.dump
# 拷出来而不是 `docker exec pg_dump > file`：那样拿到的字节流依赖 exec 的
# 管道，中途断了会得到一个**大小正常但截断**的 dump，恢复时才炸。
docker cp "$PG_CONTAINER:/tmp/pkh.dump" "$DEST/pg.dump" >/dev/null
docker exec "$PG_CONTAINER" rm -f /tmp/pkh.dump
[ -s "$DEST/pg.dump" ] || die "pg.dump 是空的"
log "  pg.dump $(du -h "$DEST/pg.dump" | cut -f1)"

# ---- 2. 原始上传文件 ----
BLOB_SHA=""
if [ -d "$BLOB_DIR" ]; then
  log "打包 $BLOB_DIR …"
  # ⚠️ 从 `data/` 的**上一级**打包并只取 `blobs/`：这样 tar 里的路径是
  #   `blobs/ab/abc.../original`，恢复时 `tar -C data_dir` 就能落回原位。
  #   直接 `tar -C "$BLOB_DIR" .` 会得到 `./ab/abc/original` —— 恢复后
  #   目录结构对不上，症状是"所有文档都提示文件缺失"。
  tar -C "$(dirname "$BLOB_DIR")" -czf "$DEST/blobs.tar.gz" blobs
  BLOB_SHA=$(sha256sum "$DEST/blobs.tar.gz" | cut -d' ' -f1)
  # 逐文件哈希表：恢复时能逐个比对（`blobs.sha256` 就是校验和清单）
  (cd "$BLOB_DIR" && find . -type f -print0 | sort -z \
     | xargs -0 sha256sum) > "$DEST/blobs.sha256"
  log "  blobs.tar.gz $(du -h "$DEST/blobs.tar.gz" | cut -f1) · $(wc -l < "$DEST/blobs.sha256") 个文件"
fi

# ---- 3. 期望的 Qdrant 点数（D25：可重建，但要记下重建目标）----
EXPECT_JSON="$DEST/qdrant_expect.json"
QDRANT_NOTE=""
if curl -sf --max-time 5 "$QDRANT_URL/collections" >/dev/null 2>&1; then
  bash deploy/_backup_lib.sh qdrant_expect > "$EXPECT_JSON" || true
  QDRANT_NOTE="已记录（重建后按它对账）"
else
  printf '{}\n' > "$EXPECT_JSON"
  QDRANT_NOTE="Qdrant 不可达，本次没记（重建目标未知）"
fi
log "Qdrant 点数：$QDRANT_NOTE"

# ---- 4. manifest（表行数 + 各文件 sha256）----
bash deploy/_backup_lib.sh manifest "$DEST" "$PG_USER" "$PG_DB" "$PG_CONTAINER" "$PG_PASS" \
  > "$DEST/manifest.json"
log "manifest.json 就绪"

# ---- 5. 立刻自检 ----
# ⚠️ 不自检就报成功 = 把"文件写出来了"当成"备份能用"。
#   而 pg_dump 写出截断文件、tar 写坏 —— 两者都不报错。
if bash deploy/_backup_lib.sh verify "$DEST"; then
  log "自检通过 ✅"
else
  die "自检**失败** —— 这份备份不能用，别留着（目录：$DEST）"
fi

# ---- 6. 轮转 ----
mapfile -t OLD < <(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort -r | tail -n +$((KEEP + 1)))
if [ "${#OLD[@]}" -gt 0 ]; then
  log "轮转：删除 ${#OLD[@]} 份旧备份（保留最近 $KEEP）"
  for d in "${OLD[@]}"; do rm -rf "$d"; warn "  已删 $(basename "$d")"; done
else
  log "轮转：无需删除（现有 ${#OLD[@]} 份超出保留上限 $KEEP）"
fi

echo
printf '\033[32m✓ 备份完成\033[0m  %s\n' "$DEST"
printf '  恢复演练：bash deploy/restore.sh --scratch %s\n' "$DEST"
printf '  ⚠️ 定期**真的**恢复一次（--scratch），否则永远不知道它能不能用。\n'