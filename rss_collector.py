#!/usr/bin/env python3
"""
RSS Collector

複数のRSSフィードから最新記事を取得し、Obsidian Wiki の raw/articles/
配下に YYYY-MM-DD-{slug}.md として保存する。

使い方:
    pip install feedparser markdownify
    python3 rss_collector.py
"""

from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Tuple, Union

try:
    import feedparser
    from markdownify import markdownify as md
except ImportError:
    sys.stderr.write(
        "必要な依存ライブラリが見つかりません。以下を実行してください:\n"
        "    pip install feedparser markdownify\n"
    )
    sys.exit(1)


# ---------- 設定 ----------

# wiki ルートは環境変数 LLM_WIKI_DIR で上書き可能。
# 未設定なら iCloud Drive 上の Obsidian vault をデフォルトとする。
# Path.home() を使うことでユーザー名をハードコードしない（daily_digest.py と統一）。
_DEFAULT_WIKI = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/llm-wiki"
WIKI_ROOT = Path(os.environ["LLM_WIKI_DIR"]) if os.environ.get("LLM_WIKI_DIR") else _DEFAULT_WIKI
OUTPUT_DIR = WIKI_ROOT / "raw" / "articles"

MAX_ENTRIES_PER_FEED = 10

# ジャンルごとのフィード定義（タグはジャンル名から自動付与）。
# 各フィードは URL 文字列、または (URL, 取得件数) のタプルで指定可能。
# タプルで指定した場合は MAX_ENTRIES_PER_FEED を上書きする。
FeedSpec = Union[str, Tuple[str, int]]

FEEDS: dict[str, list[FeedSpec]] = {
    "生成AI・LLM最新動向": [
        "https://zenn.dev/topics/llm/feed",
        "https://zenn.dev/topics/claude/feed",
        "https://zenn.dev/topics/generativeai/feed",
        "https://qiita.com/tags/llm/feed",
        "https://buttondown.com/anthropicnews/rss",
        "https://openai.com/news/rss.xml",
        "https://huggingface.co/blog/feed.xml",
        "https://blog.google/innovation-and-ai/technology/ai/rss/",
        "https://deepmind.google/blog/rss.xml",
    ],
    "個人開発・自動化": [
        "https://www.publickey1.jp/atom.xml",
        "https://b.hatena.ne.jp/hotentry/it.rss",
    ],
    "エンジニア副業・収益化": [
        "https://zenn.dev/topics/副業/feed",
        "https://qiita.com/tags/副業/feed",
        "https://zenn.dev/topics/freelance/feed",
    ],
    "金融リテラシー・投資": [
        ("https://www3.nhk.or.jp/rss/news/cat5.xml", 5),
        ("https://assets.wor.jp/rss/rdf/reuters/top.rdf", 5),
        "https://assets.wor.jp/rss/rdf/nikkei/economy.rdf",
    ],
}

GENRE_TAGS: dict[str, list[str]] = {
    "生成AI・LLM最新動向": ["生成AI", "LLM"],
    "個人開発・自動化": ["個人開発", "自動化"],
    "エンジニア副業・収益化": ["副業", "エンジニア"],
    "金融リテラシー・投資": ["金融", "投資"],
}


# ---------- ユーティリティ ----------

_SLUG_STRIP = re.compile(r"[^\w\-\u3040-\u30ff\u4e00-\u9fff]+", re.UNICODE)


def slugify(title: str, max_len: int = 60) -> str:
    """日本語を残しつつファイル名に安全な slug を作る。"""
    title = unicodedata.normalize("NFKC", title).strip()
    title = title.replace(" ", "-")
    title = _SLUG_STRIP.sub("-", title)
    title = re.sub(r"-+", "-", title).strip("-")
    if not title:
        title = "untitled"
    return title[:max_len]


def entry_date(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None) or entry.get(key) if isinstance(entry, dict) else getattr(entry, key, None)
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except Exception:
                pass
    return datetime.now(tz=timezone.utc)


def _markdownify(html: str) -> str:
    """HTML を Markdown 化する。失敗時は素の文字列を返す。"""
    if not html:
        return ""
    try:
        return md(html, heading_style="ATX").strip()
    except Exception:
        return html.strip()


_BODY_DECORATION = re.compile(r"[#*_`>\-\[\]()!~|+=\s]+")


def _effective_len(markdown_text: str) -> int:
    """Markdown 装飾・空白を除いた実質文字数を返す（本文選択の比較用）。"""
    return len(_BODY_DECORATION.sub("", markdown_text))


def entry_body_markdown(entry) -> str:
    """entry の本文を返す。

    content と summary の両方を Markdown 化し、実質文字数（装飾・空白を除いた
    長さ）が大きい方を本文として採用する。どちらも空なら空文字を返す。
    """
    content_html = ""
    if getattr(entry, "content", None):
        try:
            content_html = entry.content[0].value
        except Exception:
            content_html = ""
    summary_html = getattr(entry, "summary", "") or ""

    content_md = _markdownify(content_html)
    summary_md = _markdownify(summary_html)

    if not content_md and not summary_md:
        return ""

    # 同点時は従来どおり content を優先（>=）。
    if _effective_len(content_md) >= _effective_len(summary_md):
        return content_md
    return summary_md


def yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------- 重複チェック ----------

def build_existing_url_index(output_dir: Path) -> set[str]:
    """出力ディレクトリ内の .md の frontmatter から source_url を集める。"""
    urls: set[str] = set()
    if not output_dir.exists():
        return urls
    url_re = re.compile(r'^source_url:\s*"?([^"\n]+)"?\s*$', re.MULTILINE)
    skipped_count = 0
    for p in output_dir.glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            skipped_count += 1
            print(
                f"[rss] [WARN] URL index読み込み失敗: {p.name} "
                f"(errno={e.errno} type={type(e).__name__})",
                flush=True,
            )
            continue
        # frontmatter 部分だけ見れば十分
        if text.startswith("---"):
            end = text.find("\n---", 3)
            head = text[: end if end != -1 else 2000]
        else:
            head = text[:2000]
        m = url_re.search(head)
        if m:
            urls.add(m.group(1).strip())
    if skipped_count > 0:
        print(
            f"[rss] [INFO] URL index構築: "
            f"成功={len(urls)} 失敗={skipped_count}",
            flush=True,
        )
    return urls


# ---------- 保存 ----------

@dataclass
class SaveResult:
    fetched: int = 0
    saved: int = 0
    skipped: int = 0
    errors: int = 0
    failed_feeds: list[tuple[str, str]] = None  # (feed_url, reason)

    def __post_init__(self):
        if self.failed_feeds is None:
            self.failed_feeds = []


def save_entry(
    entry,
    genre: str,
    tags: list[str],
    output_dir: Path,
    existing_urls: set[str],
) -> str:
    """1 記事を保存。戻り値: 'saved' / 'skipped' / 'error'"""
    url = (getattr(entry, "link", "") or "").strip()
    title = (getattr(entry, "title", "") or "untitled").strip()
    if not url:
        return "error"
    if url in existing_urls:
        return "skipped"

    dt = entry_date(entry)
    date_str = dt.strftime("%Y-%m-%d")
    slug = slugify(title)
    filename = f"{date_str}-{slug}.md"
    path = output_dir / filename

    # ファイル名衝突回避
    i = 2
    while path.exists():
        path = output_dir / f"{date_str}-{slug}-{i}.md"
        i += 1

    body = entry_body_markdown(entry)

    tag_yaml = ", ".join(tags)
    frontmatter = (
        "---\n"
        f'title: "{yaml_escape(title)}"\n'
        f'source_url: "{yaml_escape(url)}"\n'
        f"date_clipped: {date_str}\n"
        f"tags: [{tag_yaml}]\n"
        f'genre: "{yaml_escape(genre)}"\n'
        "---\n\n"
    )

    content = frontmatter + f"# {title}\n\n" + (body if body else "(本文なし)") + "\n"

    try:
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"[ERROR] 書き込み失敗 {path}: {e}\n")
        return "error"

    existing_urls.add(url)
    return "saved"


# ---------- メイン ----------

def collect(feeds: dict[str, list[FeedSpec]], output_dir: Path) -> dict[str, SaveResult]:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_urls = build_existing_url_index(output_dir)

    results: dict[str, SaveResult] = {}

    for genre, urls in feeds.items():
        r = SaveResult()
        tags = GENRE_TAGS.get(genre, [genre])
        for spec in urls:
            if isinstance(spec, tuple):
                feed_url, limit = spec
            else:
                feed_url, limit = spec, MAX_ENTRIES_PER_FEED
            print(f"[{genre}] {feed_url} (最大{limit}件)", flush=True)
            try:
                parsed = feedparser.parse(feed_url)
            except Exception as e:
                reason = f"取得失敗: {e}"
                sys.stderr.write(f"  [FAIL] {feed_url}\n    -> {reason}\n")
                sys.stderr.flush()
                r.errors += 1
                r.failed_feeds.append((feed_url, reason))
                continue
            if parsed.bozo and not parsed.entries:
                reason = f"パース失敗: {getattr(parsed, 'bozo_exception', '')}"
                sys.stderr.write(f"  [FAIL] {feed_url}\n    -> {reason}\n")
                sys.stderr.flush()
                r.errors += 1
                r.failed_feeds.append((feed_url, reason))
                continue

            for entry in parsed.entries[:limit]:
                r.fetched += 1
                status = save_entry(entry, genre, tags, output_dir, existing_urls)
                if status == "saved":
                    r.saved += 1
                elif status == "skipped":
                    r.skipped += 1
                else:
                    r.errors += 1
        results[genre] = r

    return results


def print_report(results: dict[str, SaveResult]) -> None:
    print("\n========== 実行ログ ==========")
    total = SaveResult()
    all_failed: list[tuple[str, str, str]] = []  # (genre, feed_url, reason)
    for genre, r in results.items():
        print(
            f"[{genre}] 取得={r.fetched} 保存={r.saved} "
            f"スキップ={r.skipped} エラー={r.errors}"
        )
        total.fetched += r.fetched
        total.saved += r.saved
        total.skipped += r.skipped
        total.errors += r.errors
        for feed_url, reason in r.failed_feeds:
            all_failed.append((genre, feed_url, reason))
    print("------------------------------")
    print(
        f"合計: 取得={total.fetched} 保存={total.saved} "
        f"スキップ={total.skipped} エラー={total.errors}"
    )
    if all_failed:
        print("\n---------- 失敗フィード ----------")
        for genre, feed_url, reason in all_failed:
            print(f"[{genre}] {feed_url}")
            print(f"    -> {reason}")
    print(f"\n出力先: {OUTPUT_DIR}")


def main() -> int:
    results = collect(FEEDS, OUTPUT_DIR)
    print_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
