#!/usr/bin/env bash
# 🔑 恢复（阶段 7 · D25）。**默认恢复到临时库并逐表对账**。
#
# ## 为什么默认不碰生产库
#
# 恢复是**破坏性**操作：往一个已经有数据的库里 `pg_restore` 会撞唯一键，
# 而"撞了之后怎么办"没人想得出。所以默认 `--scratch`：
# 建一个临时库 → 恢复 → 逐表对账 → **删掉临时库**。
# 这是"定期演练"的入口，也是唯一能证明"备份能用"的方式
# —— 没恢复过的备份只是一堆文件。
#
# ## 两种模式
#
# ```bash
# # ① 演练（推荐，每个月做一次）：临时库 → 对账 → 删掉
# bash deploy/restore.sh --scratch data/backups/20261004-101500
#
# # ② 真恢复（覆盖生产库）：需要 --force，且必须先停 API/worker
# bash deploy/restore.sh --force --into pkh data/backups/20261004-101500
# ```
#
# ## 对账什么
#
# | 对账项 | 判据 |
# |---|---|
# | 表行数 | 每张表 == manifest 里的行数（`table_rows`） |
# | 原始文件 | 逐个 sha256 == `blobs.sha256`（不是只比数量） |
# | Qdrant | ⚠️ **不比** —— D25 说不备份索引；比了必然不一致（点还在原库） |
#
# 🔑 恢复完**必须重建索引**（`POST /admin/reindex` 或 KIND_REINDEX），
# 而重建后的点数应该等于 manifest 的 `qdrant_expect` —— 那才是
# "索引可重建"这句话的验收方式。
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && { set -a; . ./.env; set +a; }

PG_USER="${POSTGRES_USER:-pkh}"
PG_CONTAINER="${PG_CONTAINER:-pkh-postgres}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
BLOB_DIR="${PKH_DATA_DIR:-data}/blobs"

log()  { printf '\033[36m→\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# shellcheck disable=SC1091
source deploy/_backup_lib.sh

MODE="scratch"
INTO=""
FORCE=0
KEEP_BLOBS=0
SRC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --scratch)  MODE="scratch"; shift ;;
    --force)    FORCE=1; shift ;;
    --into)     INTO="$2"; MODE="into"; shift 2 ;;
    --into-blobs) KEEP_BLOBS=1; shift ;;   # 把原件也恢复到 data/blobs
    -h|--help)  sed -n '2,34p' "$0"; exit 0 ;;
    *) SRC="$1"; shift ;;
  esac
done
[ -n "$SRC" ] || die "用法：$0 --scratch <备份目录>  /  $0 --force --into <库名> <备份目录>"
[ -f "$SRC/manifest.json" ] || die "$SRC/manifest.json 不存在"
PG_DB="$([ "$MODE" = into ] && echo "${INTO:-}" || echo "")"

TABLES=$(python3 -c "
import json,sys
m=json.load(open('$SRC/manifest.json'))
print('\n'.join(f'{t}\t{c}' for t,c in sorted(m['table_rows'].items())))")

# ---------------------------------------------------------------
# ① 完整性：先 verify 再动手
# ---------------------------------------------------------------
log "校验备份完整性…"
bash deploy/_backup_lib.sh verify "$SRC" || die "备份本身有问题，中止（先修备份或换一份）"

SCRATCH_DB=""
cleanup() {
  if [ -n "$SCRATCH_DB" ]; then
    log "删除临时库 $SCRATCH_DB…"
    docker exec -e PGPASSWORD="${POSTGRES_PASSWORD:-}" "$PG_CONTAINER" \
      dropdb -U "$PG_USER" --if-exists "$SCRATCH_DB" >/dev/null 2>&1 || true
  fi
  [ -n "${TMP_DIR:-}" ] && rm -rf "$TMP_DIR"
}
trap cleanup EXIT

if [ "$MODE" = scratch ]; then
  # 🔑 库名带时间戳：固定名会让"上次残留的临时库"让这次恢复看起来成功
  #   （pg_restore 撞键会失败，但如果残留库里数据一样多，count 恰好对上 ——
  #   于是对账"通过"而实际恢复的是残留数据）。
  SCRATCH_DB="pkh_restore_$(date +%H%M%S)"
  log "建临时库 $SCRATCH_DB…"
  docker exec -e PGPASSWORD="${POSTGRES_PASSWORD:-}" "$PG_CONTAINER" \
    dropdb -U "$PG_USER" --if-exists "$SCRATCH_DB" >/dev/null 2>&1 || true
  docker exec -e PGPASSWORD="${POSTGRES_PASSWORD:-}" "$PG_CONTAINER" \
    createdb -U "$PG_USER" "$SCRATCH_DB"
  PG_DB="$SCRATCH_DB"
  echo
  printf '\033[33m⚠ 这是演练模式：会恢复进临时库 %s 并在结束后删掉它。\033[0m\n' "$SCRATCH_DB"
  printf '   真正恢复生产库请显式加 --force --into <库名>。\n\n'
else
  [ "$FORCE" = 1 ] || die "恢复生产库需要 --force（不想误操作就改用 --scratch）"
  log "⚠ 真恢复到生产库 $PG_DB —— 请确认 API 与 worker 已停"
  pgrep -af "uvicorn services.api.main|services.worker.main" \
    && die "API/worker 还在跑。先 bash deploy/dev.sh stop —— \
恢复时写进来的数据会被它们读到（半恢复状态比不恢复更难查）"
fi

# ---------------------------------------------------------------
# ② 恢复数据库
# ---------------------------------------------------------------
log "恢复数据库 → $PG_DB…"
TMP_DIR=$(mktemp -d -t pkh-restore-XXXX)
cp "$SRC/pg.dump" "$TMP_DIR/pg.dump"
docker cp "$TMP_DIR/pg.dump" "$PG_CONTAINER:/tmp/pkh-restore.dump" >/dev/null
# ⚠️ `--clean --if-exists`：`--force` 模式下库里已有数据，不加会撞唯一键；
#   而撞键的报错是 "duplicate key value"，完全指不到"这是恢复"。
docker exec -e PGPASSWORD="${POSTGRES_PASSWORD:-}" "$PG_CONTAINER" \
  pg_restore -U "$PG_USER" -d "$PG_DB" --clean --if-exists --no-owner \
  /tmp/pkh-restore.dump
docker exec "$PG_CONTAINER" rm -f /tmp/pkh-restore.dump
log "  pg_restore 完成"

# ---------------------------------------------------------------
# ③ 逐表对账
# ---------------------------------------------------------------
log "逐表对账（manifest vs 实际）…"
# ⚠️ 只比 manifest 里**记过的**表：恢复库里可能多出 `alembic_version` 之类
#   （pg_dump 带 schema 但不一定带这些表），多出来的不是"少数据"，
#   混进判据会让人以为备份坏了。
DIFF=$(python3 - "$SRC/manifest.json" "$PG_CONTAINER" "$PG_USER" "$PG_DB" \
       "${POSTGRES_PASSWORD:-}" <<'PY'
import json, subprocess, sys
path, container, user, db, pw = sys.argv[1:6]
want = json.load(open(path))["table_rows"]
sql = (
    "SELECT relname || chr(9) || (xpath('/row/c/text()', query_to_xml("
    "'SELECT count(*) AS c FROM ' || quote_ident(relname), false, true, '')))[1]::text "
    "FROM pg_class WHERE relkind='r' AND relnamespace='public'::regnamespace "
    "AND relname NOT LIKE 'pg_%'"
)
out = subprocess.run(
    ["docker", "exec", "-e", "PGPASSWORD=" + pw, container,
     "psql", "-U", user, "-d", db, "-tAc", sql],
    capture_output=True, text=True,
)
if out.returncode != 0:
    print("\x00psql 失败：" + out.stderr.strip()[:200])
    raise SystemExit(2)
got = {}
for line in out.stdout.splitlines():
    parts = line.split("\t")
    if len(parts) == 2 and parts[0]:
        got[parts[0]] = int(parts[1])
bad = [
    "{}: 记 {} 行 → 实际 {} 行".format(
        t, c, got.get(t, "（恢复库里没有这张表）"))
    for t, c in sorted(want.items()) if got.get(t) != c
]
print(str(len(bad)))
for b in bad:
    print(b)
PY
)
RC=$?
[ "$RC" = 2 ] && die "对账时 psql 失败（上面是原始报错）"
NBAD=$(printf '%s\n' "$DIFF" | head -1)
DETAIL_TEXT=$(printf '%s\n' "$DIFF" | tail -n +2)
TBL_N=$(python3 -c "import json;print(len(json.load(open('$SRC/manifest.json'))['table_rows']))")

if [ "$NBAD" != "0" ]; then
  while IFS= read -r line; do warn "$line"; done <<< "$DETAIL_TEXT"
  die "对账失败：$NBAD/$TBL_N 张表对不上"
fi
printf '\033[32m✓\033[0m %s 张表行数全部对上\n' "$TBL_N"

# ---------------------------------------------------------------
# ④ 原始文件逐个校验
# ---------------------------------------------------------------
N_FILES=$(python3 -c "import json;print((json.load(open('$SRC/manifest.json'))['blobs'] or {}).get('files',0))")
if [ "${N_FILES:-0}" -gt 0 ]; then
  log "校验原始文件（$N_FILES 个，逐个 sha256）…"
  # 演练模式：解到临时目录再比。⚠️ `tar -C data` 的路径是 `blobs/...`，
  #   所以解出来的顶层就叫 `blobs`（与 $BLOB_DIR 的目录名一致）——
  #   这一步同时验了"tar 里的路径结构对不对"，那是 `--into-blobs` 能不能
  #   直接落回原位的关键。
  TAR_DEST="$TMP_DIR/restore"
  mkdir -p "$TAR_DEST"
  tar -C "$TAR_DEST" -xzf "$SRC/blobs.tar.gz"
  CHK=$(cd "$TAR_DEST/blobs" && sha256sum -c "$SRC/blobs.sha256" 2>&1 \
        | grep -c ": FAILED$" || true)
  if [ "${CHK:-0}" -ne 0 ]; then
    die "原始文件校验失败：$CHK 个文件的 sha256 对不上"
  fi
  printf '\033[32m✓\033[0m %s 个原始文件 sha256 全部对上\n' "$N_FILES"

  if [ "$KEEP_BLOBS" = 1 ]; then
    [ "$MODE" = into ] || die "--into-blobs 只能配合真恢复用（演练不要动真实目录）"
    warn "把 $N_FILES 个文件恢复到 $BLOB_DIR …"
    cp -a "$TAR_DEST/blobs/." "$BLOB_DIR/"
    log "已恢复（同名同内容时是幂等的：内容寻址）"
  fi
fi

# ---------------------------------------------------------------
# ⑤ 重建索引（D25 的后半句：Qdrant 不备份 → 靠重建）
# ---------------------------------------------------------------
QEXP=$(python3 -c "import json;print(json.dumps(json.load(open('$SRC/manifest.json')).get('qdrant_expect') or {},ensure_ascii=False))")
echo
printf '\033[1m恢复后的收尾\033[0m\n'
printf '  1. 🔑 重建索引（Qdrant 不在备份里，D25）：\n'
printf '       bash deploy/dev.sh start && curl -X POST -H "Authorization: Bearer $PKH_API_KEY" \\\n'
printf '         http://127.0.0.1:8010/api/v1/admin/reindex\n'
if [ "$QEXP" != "{}" ]; then
  printf '  2. 重建后按这个点数对账（超出 5%% 要查原因）：\n'
  printf '       %s\n' "$QEXP" | sed 's/^/       /'
else
  printf '  2. （这份备份没记 Qdrant 点数，对账时自己数一遍）\n'
fi
printf '  3. 跑一次完整验收：uv run pytest && cd web && npm test\n'

if [ "$MODE" = scratch ]; then
  echo
  printf '\033[32m✓ 演练通过\033[0m —— 这份备份**确实能恢复**。临时库将在退出时删除。\n'
  printf '  建议每月演练一次，并把它记到运维日志里（日期 + 备份目录）。\n'
fi