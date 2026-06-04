#!/bin/bash
set -u
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="${LANG:-ja_JP.UTF-8}"

BRIDGE_DIR="/Users/mini2020/my-scripts/alpha-bridge"
REQ_DIR="$BRIDGE_DIR/requests"
RES_DIR="$BRIDGE_DIR/responses"
PROC_DIR="$BRIDGE_DIR/processing"
LOG="$BRIDGE_DIR/bridge.log"
SCORE_SH="/Users/mini2020/my-scripts/score_se_value.sh"

echo "[bridge] $(date '+%F %T') 起動 (WatchPathsトリガ) pid=$$" >> "$LOG"

# requests/ 内の .prompt を処理（複数あっても順に。通常は1個）
shopt -s nullglob
found=0
for req in "$REQ_DIR"/*.prompt; do
  found=1
  id="$(basename "$req" .prompt)"
  echo "[bridge] $(date '+%F %T') 処理開始 id=$id" >> "$LOG"

  # 二重処理防止：processing/へ mv（アトミック）。失敗したら他プロセスが処理中とみなしskip
  if ! mv "$req" "$PROC_DIR/$id.prompt" 2>/dev/null; then
    echo "[bridge] $(date '+%F %T') skip(既に処理中?) id=$id" >> "$LOG"
    continue
  fi

  # score_se_value.sh にプロンプトを stdin で渡し、stdout(JSON)を一時ファイルへ
  tmp_out="$RES_DIR/.$id.json.tmp"
  "$SCORE_SH" < "$PROC_DIR/$id.prompt" > "$tmp_out" 2>>"$LOG"
  rc=$?

  if [ $rc -eq 0 ] && [ -s "$tmp_out" ]; then
    # アトミックに正式名へ（書き込み途中をdigestが読まないように）
    mv "$tmp_out" "$RES_DIR/$id.json"
    echo "[bridge] $(date '+%F %T') 完了 id=$id rc=0 → $RES_DIR/$id.json" >> "$LOG"
  else
    rm -f "$tmp_out"
    echo "[bridge] $(date '+%F %T') [WARN] α失敗 id=${id} rc=${rc}（digest側はタイムアウトでβ代替）" >> "$LOG"
  fi

  # 処理済みプロンプトは消す（ゴミ残留防止）
  rm -f "$PROC_DIR/$id.prompt"
done

[ $found -eq 0 ] && echo "[bridge] $(date '+%F %T') 対象 .prompt なし（空振り起動）" >> "$LOG"
echo "[bridge] $(date '+%F %T') 終了 pid=$$" >> "$LOG"
exit 0
