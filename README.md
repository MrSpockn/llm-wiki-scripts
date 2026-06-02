# llm-wiki-scripts

個人知識ベース「LLM Wiki」を**毎朝自動で育てる**ための運用スクリプト群。
Mac mini 上の定期実行で、関心分野の RSS を収集し、Claude Code で wiki 化し、
日次ダイジェストと週次レポートを生成する——という一連のパイプラインを構成する。

## 何をするものか

```
RSS フィード
   │  rss_collector.py（毎朝・純 Python）
   ▼
raw/articles/*.md  ← 収集した記事
   │  compile_wiki.sh（毎朝・Claude Code headless）
   ▼
wiki/concepts/*.md ← 概念ごとに整理された wiki ページ
   │  daily_digest.py / weekly_report.py（純 Python）
   ▼
日次ダイジェスト・週次レポート
```

収集（Python）と wiki 化（Claude Code）を分離し、LLM 呼び出しを
`compile_wiki.sh` の 1 本に閉じているのが構成上の要点。これにより
収集・スコアリング・レポート生成はコストゼロ・決定論的に動き、
意味的な統合だけを LLM に任せている。

## 構成

| ファイル | 役割 | LLM |
|---|---|---|
| `rss_collector.py` | RSS 収集 → `raw/articles/` に Markdown 保存 | — |
| `compile_wiki.sh` | 未統合記事を Claude Code で wiki ページ化 | ✓ |
| `daily_digest.py` | 当日新着をスコアリングし日次ダイジェスト生成 | — |
| `weekly_report.py` | 週次レポート（ブログネタシート等）生成 | — |
| `CLAUDE.md` | Claude Code 用の作業定義 | — |

## セットアップ

```bash
pip install feedparser markdownify

# wiki の場所（未設定なら $HOME 配下の iCloud Obsidian vault を使う）
export LLM_WIKI_DIR="$HOME/path/to/your/llm-wiki"
```

## 実行

```bash
python3 rss_collector.py
bash    compile_wiki.sh
python3 daily_digest.py
python3 weekly_report.py
```

定期実行は cron または launchd で構成する。

## 設計メモ

- **パスはハードコードしない**: ユーザー名を含む絶対パスを避け、
  `Path.home()` / `$HOME` と環境変数 `LLM_WIKI_DIR` で解決する。
- **収集と統合の分離**: 純 Python 処理と LLM 処理を別プロセスにし、
  障害の切り分けとコスト管理を容易にしている。

---

This work is shared as a reference implementation.
🖖 Live long and learn.
