# my-scripts — LLM Wiki 運用スクリプト群

このディレクトリは、個人知識ベース「LLM Wiki」を自動で育てるための
スクリプト群です。Mac mini 上で定期実行され、RSS 収集 → wiki 化 →
日次/週次レポート生成までを担います。

> このリポジトリには運用コードのみを含みます。マシン固有の設定値・
> 認証情報・インシデント履歴・実行ログは `.gitignore` で除外しています。

## スクリプトの役割

| ファイル | 役割 | LLM 呼び出し |
|---|---|---|
| `rss_collector.py` | 複数の RSS フィードを収集し、`raw/articles/` に Markdown 保存 | なし（純 Python） |
| `compile_wiki.sh` | Claude Code（headless）で未統合記事を wiki ページ化 | あり |
| `daily_digest.py` | 当日の新着記事をスコアリングし日次ダイジェスト生成 | なし（純 Python） |
| `weekly_report.py` | 週次レポート（ブログネタシート等）を生成 | なし（純 Python） |

## パス設定

wiki 本体の場所は環境変数 `LLM_WIKI_DIR` で指定できます。未設定の場合、
各スクリプトは `$HOME` 配下の iCloud Drive 上 Obsidian vault を
デフォルトとして解決します（ユーザー名はハードコードしていません）。

```bash
# 例: wiki の場所を明示する場合
export LLM_WIKI_DIR="$HOME/path/to/your/llm-wiki"
```

`compile_wiki.sh` は `CLAUDE_BIN` で Claude Code の実行パスも上書き可能です。

## 依存ライブラリ

```bash
pip install feedparser markdownify
```

## 実行

```bash
python3 rss_collector.py      # RSS 収集
bash    compile_wiki.sh        # wiki 化（Claude Code headless）
python3 daily_digest.py        # 日次ダイジェスト
python3 weekly_report.py       # 週次レポート
```

## Claude Code への作業指示

このディレクトリで Claude Code を使う際の前提:

- `raw/` は RSS 収集の出力先。人間・スクリプトが書き込み、編集はしない。
- wiki 本体のスキーマ・コマンド定義（ingest/compile/query/lint）は、
  wiki ディレクトリ側の `CLAUDE.md` を参照する。
- スクリプトを編集する際は、マシン固有のパス・ユーザー名・認証情報を
  コードに直接書かない（環境変数 or `$HOME`/`Path.home()` を使う）。
