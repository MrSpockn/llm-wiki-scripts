#!/bin/bash
# score_se_value.sh
# stdin で受け取った判定プロンプトを Claude (Sonnet) に渡し、
# SE価値ランキングの JSON を stdout に返す。
# daily_digest.py から subprocess 経由で呼ばれることを想定。

set -u
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# claude は LANG 未設定だと起動に失敗するため明示設定（インシデント履歴の教訓）
export LANG="${LANG:-ja_JP.UTF-8}"

CLAUDE_BIN="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"
MODEL="${ALPHA_MODEL:-claude-sonnet-4-6}"

if [ ! -x "$CLAUDE_BIN" ]; then
  echo "[score-se] [ERROR] claude が見つかりません: $CLAUDE_BIN" >&2
  exit 1
fi

# stdin の全内容をプロンプトとして claude に渡す。
# 読み取り専用判定なので --dangerously-skip-permissions は付けない。
"$CLAUDE_BIN" -p "$(cat)" --model "$MODEL"
exit $?
