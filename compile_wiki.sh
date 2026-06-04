#!/bin/bash
# llm-wiki の compile を Claude Code headless で実行
# CLAUDE.md の「compile」定義に従って raw/articles/ の未統合記事を wiki/ に反映する

set -u
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# wiki ディレクトリは環境変数 LLM_WIKI_DIR で上書き可能。
# 未設定なら $HOME 配下の iCloud Drive Obsidian vault をデフォルトとする。
WIKI_DIR="${LLM_WIKI_DIR:-$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/llm-wiki}"
CLAUDE_BIN="${CLAUDE_BIN:-/opt/homebrew/bin/claude}"

echo ""
echo "========== compile =========="
echo "[compile] $(date '+%Y-%m-%d %H:%M:%S') 開始"

if [ ! -x "$CLAUDE_BIN" ]; then
  echo "[compile] [ERROR] claude が見つかりません: $CLAUDE_BIN"
  exit 1
fi

cd "$WIKI_DIR" || { echo "[compile] [ERROR] cd 失敗: $WIKI_DIR"; exit 1; }

PROMPT="CLAUDE.md に定義された compile コマンドを実行してください。手順: (1) raw/articles/ 内のファイルで、まだどの wiki ページの sources にも含まれていないものを未コンパイル記事として検出する。(2) 各未コンパイル記事の内容を読み、新しい概念は wiki/concepts/ に新規ページを作成、既存概念に関連する内容は該当ページを更新する。(3) 新規・更新ページの frontmatter (title/tags/sources/updated) を必ず維持する。(4) 処理結果を「[compile] 新規=N 更新=M スキップ=K」の形式で最後に 1 行で報告する。"

"$CLAUDE_BIN" -p "$PROMPT" --dangerously-skip-permissions
rc=$?
echo "[compile] $(date '+%Y-%m-%d %H:%M:%S') 終了 (exit=$rc)"
exit $rc
