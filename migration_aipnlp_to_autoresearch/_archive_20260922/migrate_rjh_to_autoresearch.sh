#!/usr/bin/env bash
# migrate_rjh_to_autoresearch.sh
# 把个人空间从 your-user 迁到 hadoop-autoresearch。
# 前提：hadoop-autoresearch 目录已建好（见 TT_工单），且在「挂 autoresearch 组盘的容器」内执行。
#
# 安全约定（勿改）：
#   .rustup / .cache 是多 agent 共享工具链 —— 不迁、不碰；新环境继续用绝对路径调用。
#   tofu/logs 默认排除（重建可再生成，约 191M）。
#
# 决策②开关（tofu 是否整体随迁）：
#   MIGRATE_TOFU=1  → 连 tofu/（含 data/conversations、data/memories、.tofu）一起搬（默认）
#   MIGRATE_TOFU=0  → 只搬代码/数据，排除 tofu 项目本体
set -euo pipefail

SRC="/path/to/your/data"
DST="/mnt/your-fs"
MIGRATE_TOFU="${MIGRATE_TOFU:-1}"
LOG="/tmp/migrate_$(date +%Y%m%d_%H%M%S).log"

if [[ ! -d /mnt/your-fs ]]; then
  echo "[!] 目标 hadoop-autoresearch 不存在，先提 TT 建目录（见 TT_工单_dolphinfs_autoresearch.md）" >&2
  exit 1
fi

EXCLUDES=(
  --exclude='.rustup/***'
  --exclude='.cache/***'
  --exclude='**/__pycache__/**'
  --exclude='**/.git/objects/**'
  --exclude='tofu/logs/***'
)
if [[ "$MIGRATE_TOFU" == "0" ]]; then
  EXCLUDES+=( --exclude='tofu/***' )
  echo "[*] MIGRATE_TOFU=0 → 排除 tofu 项目本体"
fi

mkdir -p "$DST"
echo "[*] $SRC -> $DST"
echo "[*] log: $LOG"

rsync -aP --human-readable "${EXCLUDES[@]}" "$SRC/" "$DST/" 2>&1 | tee "$LOG"

echo "[*] 校验文件数（源 vs 目标，已剔除共享工具链/排除项）"
s=$(cd "$SRC" && find . -path ./.rustup -prune -o -path ./.cache -prune -o -path ./tofu/logs -prune -o -type f -print | wc -l)
d=$(cd "$DST" && find . -type f -print | wc -l)
echo "    src files=$s   dst files=$d"
[[ "$MIGRATE_TOFU" == "0" ]] && echo "    (注：已排除 tofu，数量不一致属正常)"
echo "[✓] 完成。请抽查关键文件后再清理源。"
