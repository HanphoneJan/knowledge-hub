#!/usr/bin/env bash
# 备份的**公共部分**（被 `backup.sh` 与 `restore.sh` 一起 source，
# 也可以单独当校验器用：`bash deploy/_backup_lib.sh verify <目录>`）。
#
# 为什么单独一个文件：备份与恢复要读同一份 manifest、用同一套对账口径。
# 两边各写一遍的话，"备份时记的表名"与"恢复时数的表名"迟早对不上，
# 而症状是"少了一张表的数据" —— 那正是最坏的一类 bug。
#
# 子命令：
#   manifest <目录> <pg_user> <pg_db> <容器> <密码>   生成 manifest.json 到 stdout
#   qdrant_expect                                   各 collection 的点数（JSON）
#   verify <目录>                                   校验备份是否完整可用
#   table_counts <用户> <库> <容器> <密码>            精确行数（JSON，manifest 与对账共用）
set -euo pipefail

_pkl_die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------
# 精确行数：备份与"恢复后对账"必须用**同一个**函数，否则口径会漂
# ---------------------------------------------------------------
# ⚠️ 用 `count(*)` 而不是 `pg_stat_user_tables.n_live_tup`（估算值）：
#   拿估算当判据的后果是"备份对不对"这个判断本身就不可靠
#   （ANALYZE 没跑过时它能差好几倍，于是永远"通过"或永远"失败"）。
# ⚠️ 表名用 `%I`/`%L` 转义：表名来自 `pg_class`，但仍要防住
#   含引号的奇怪名字（迁移脚本能造出这种名字）。
_pkl_table_counts() {
  local user="$1" db="$2" ctr="$3" pass="$4"
  local sql
  sql=$(docker exec -e PGPASSWORD="$pass" "$ctr" \
    psql -U "$user" -d "$db" -tAc "
      SELECT string_agg(format('SELECT %L AS t, count(*)::bigint AS c FROM %I',
                               relname, relname), ' UNION ALL ')
      FROM pg_class
      WHERE relkind = 'r'
        AND relnamespace = 'public'::regnamespace
        AND relname NOT LIKE 'pg_%';")
  [ -n "$sql" ] || { echo '{}'; return 0; }
  docker exec -e PGPASSWORD="$pass" "$ctr" \
    psql -U "$user" -d "$db" -tAc "$sql" \
  | python3 -c '
import json, sys
out = {}
for line in sys.stdin:
    parts = line.rstrip("\n").split("|")
    if len(parts) == 2 and parts[0]:
        out[parts[0]] = int(parts[1])
print(json.dumps(out))
'
}

# ---------------------------------------------------------------
# Qdrant 各 collection 的点数（D25：索引可重建，但要记下重建目标）
# ---------------------------------------------------------------
_pkl_qdrant_expect() {
  local base="${QDRANT_URL:-http://127.0.0.1:6333}"
  local names
  names=$(curl -sf --max-time 5 "$base/collections" \
    | tr ',' '\n' | sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  [ -n "$names" ] || { echo "{}"; return 0; }
  printf '{'
  local first=1
  for c in $names; do
    # ⚠️ `points_count` 用 **exact=true**：`indexed_vectors_count` 只是
    #   已建索引的部分（新建的 collection 会是 0），拿它当"重建目标"
    #   会让重建结果"对不上"而其实是对的。
    local n
    n=$(curl -sf --max-time 30 -X POST "$base/collections/$c/points/count" \
        -H 'Content-Type: application/json' -d '{"exact":true}' \
        | sed -n 's/.*"count"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p')
    [ "$first" = 1 ] || printf ','
    first=0
    printf '"%s":%s' "$c" "${n:-0}"
  done
  printf '}\n'
}

# ---------------------------------------------------------------
# manifest
# ---------------------------------------------------------------
_pkl_manifest() {
  local dir="$1" user="$2" db="$3" ctr="$4" pass="$5"
  local tables; tables=$(_pkl_table_counts "$user" "$db" "$ctr" "$pass")
  local blob_sha="" blob_n=0 blob_bytes=0
  if [ -f "$dir/blobs.tar.gz" ]; then
    blob_sha=$(sha256sum "$dir/blobs.tar.gz" | cut -d' ' -f1)
    blob_bytes=$(stat -c%s "$dir/blobs.tar.gz")
  fi
  [ -f "$dir/blobs.sha256" ] && blob_n=$(wc -l < "$dir/blobs.sha256")
  local pg_sha; pg_sha=$(sha256sum "$dir/pg.dump" | cut -d' ' -f1)
  local pg_bytes; pg_bytes=$(stat -c%s "$dir/pg.dump")

  python3 - "$dir" "$pg_sha" "$pg_bytes" "$blob_sha" "$blob_bytes" "$blob_n" "$tables" <<'PY'
import datetime as dt, json, pathlib, sys
d, pg_sha, pg_bytes, blob_sha, blob_bytes, blob_n, tables = sys.argv[1:8]
path = pathlib.Path(d)
qexp = {}
if (path / "qdrant_expect.json").exists():
    try:
        qexp = json.loads((path / "qdrant_expect.json").read_text() or "{}")
    except json.JSONDecodeError:
        # ⚠️ 解析不了就留空，**不要**把异常抛到备份流程外面去：
        #   它只是"重建目标"这份附加信息，丢了不影响备份可用性。
        qexp = {}
out = {
    "format": 1,
    "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    "pg": {"file": "pg.dump", "format": "custom",
           "sha256": pg_sha, "bytes": int(pg_bytes)},
    "blobs": ({"file": "blobs.tar.gz", "sha256": blob_sha,
               "bytes": int(blob_bytes), "files": int(blob_n),
               "hashes_file": "blobs.sha256"} if blob_sha else {"files": 0}),
    "table_rows": json.loads(tables),
    "qdrant_expect": qexp,
}
print(json.dumps(out, ensure_ascii=False, indent=2))
PY
}

# ---------------------------------------------------------------
# verify：这份备份**能不能用**
# ---------------------------------------------------------------
_pkl_verify() {
  local dir="$1"
  [ -d "$dir" ] || _pkl_die "目录不存在：$dir"
  [ -f "$dir/manifest.json" ] || _pkl_die "没有 manifest.json（不是本脚本生成的备份？）"

  python3 - "$dir" "${PG_CONTAINER:-pkh-postgres}" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys
d = pathlib.Path(sys.argv[1])
container = sys.argv[2]
m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
bad = []

def sha(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()

# ① 文件在不在 + 哈希对不对（截断的 pg_dump 哈希必不符）
for key in ("pg", "blobs"):
    spec = m.get(key) or {}
    if not spec.get("sha256"):
        continue
    f = d / spec["file"]
    if not f.exists():
        bad.append(f"缺文件 {spec['file']}")
        continue
    got = sha(f)
    if got != spec["sha256"]:
        bad.append(f"{spec['file']} 哈希不符（被截断或损坏）：{got[:12]}…")

# ② tar 能不能列出来（写盘时就损坏的情况，哈希可能仍对）
blobs = m.get("blobs") or {}
if blobs.get("sha256"):
    r = subprocess.run(["tar", "-tzf", str(d / "blobs.tar.gz")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        bad.append(f"blobs.tar.gz 解不开：{r.stderr.strip()[:120]}")
    else:
        n = sum(1 for line in r.stdout.splitlines() if line and not line.endswith("/"))
        # ⚠️ 这里只比**数量**；逐个哈希在 restore.sh（那要几秒，verify 要随手能跑）
        if n != blobs.get("files"):
            bad.append(f"tar 里 {n} 个文件，manifest 记 {blobs.get('files')} 个")

# ③ pg_dump 能不能读出表清单（`pg_restore -l` 不需要连库）
if (d / "pg.dump").exists():
    r = subprocess.run(["docker", "exec", "-i", container, "pg_restore", "-l"],
                       input=(d / "pg.dump").read_bytes(), capture_output=True)
    if r.returncode != 0:
        bad.append("pg_restore -l 读不出表清单（dump 文件坏了）")
    elif b"TABLE DATA" not in r.stdout:
        # ⚠️ 没报错的空 dump 也是坏的：`-Fc` 的空库 dump 仍会成功退出。
        #   判据是"里面得有 TABLE DATA 条目"。
        bad.append("pg_restore -l 里没有 TABLE DATA —— dump 是空的")

# ④ 备份那一刻有没有空表（提示，不算失败）
rows = m.get("table_rows") or {}
empty = sorted(t for t, c in rows.items() if c == 0)
print(f"\033[2m备份时间 {m.get('created_at')} · {len(rows)} 张表 · "
      f"blobs {blobs.get('files', 0)} 个文件\033[0m")
if empty:
    print(f"\033[33m!\033[0m 备份时这些表是 0 行：{', '.join(empty)}")

if bad:
    for b in bad:
        print(f"\033[31m✗ {b}\033[0m")
    sys.exit(1)
print("\033[32m✓ 完整可用\033[0m")
PY
}

# 🔑 **只有直接执行本文件时才分发子命令**。
#   `backup.sh` / `restore.sh` 会 `source` 本文件，那时 `$1` 是**调用者的**
#   参数（如 `--keep`/`--scratch`），直接分发会把它们当子命令 ——
#   症状是备份脚本一启动就打印"用法：manifest|qdrant_expect|…"然后什么都不做。
#   判据：source 时 `$0` 是调用者、`BASH_SOURCE[0]` 是本文件，两者不等。
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1:-}" in
    manifest)      shift; _pkl_manifest "$@" ;;
    qdrant_expect) _pkl_qdrant_expect ;;
    table_counts)  shift; _pkl_table_counts "$@" ;;
    verify)        shift; _pkl_verify "$@" ;;
    *) echo "用法：$0 manifest|qdrant_expect|table_counts|verify ..." >&2; exit 2 ;;
  esac
fi