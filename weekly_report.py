#!/usr/bin/env python3
"""
Weekly Insight Report Generator

当週（月〜日）の新着記事 + Wiki の更新・健全性を 1 枚にまとめる。

- raw/articles/ から当週の記事をスコアリングしてジャンル別トップ10件
- wiki/concepts/ で当週 updated: のページ一覧（+ 1行説明）
- ブログ記事ネタ 3 件（本文 200 文字未満は除外）
- lint 結果（孤立ページ / 壊れた [[ ]] / 新規作成で未リンクの概念）

出力: outputs/weekly-YYYY-WNN.md
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# daily_digest.py と同じディレクトリに置かれる前提で import する
sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_digest as dd  # noqa: E402


# ---------- 設定 ----------

HOME = Path.home()
WIKI_ROOT = HOME / "Library/Mobile Documents/iCloud~md~obsidian/Documents/llm-wiki"
ARTICLES_DIR = WIKI_ROOT / "raw" / "articles"
OUTPUTS_DIR = WIKI_ROOT / "outputs"
WIKI_DIR = WIKI_ROOT / "wiki"
CONCEPTS_DIR = WIKI_DIR / "concepts"
INTEREST_PROFILE = WIKI_ROOT / "interest_profile.md"
RSS_LOG = HOME / "my-scripts" / "rss_collector.log"  # 死活監視の集計元（compile/α の成否）

# ジャンル別トップ表示の上限（合計 10 件）
TOTAL_TOP_ARTICLES = 10
PER_GENRE_CAP = 4  # 1 ジャンルが全部を占有しないようにする
GENRE_ORDER = [
    "生成AI・LLM最新動向",
    "エンジニア副業・収益化",
    "金融リテラシー・投資",
]


# ---------- 週の範囲 ----------

@dataclass
class WeekRange:
    iso_year: int
    iso_week: int
    monday: date
    sunday: date  # 週末

    @property
    def label(self) -> str:
        return f"W{self.iso_week:02d}"

    @property
    def file_stem(self) -> str:
        return f"weekly-{self.iso_year}-{self.label}"


def current_week(today: date) -> WeekRange:
    iso_year, iso_week, iso_day = today.isocalendar()
    monday = today - timedelta(days=iso_day - 1)
    return WeekRange(
        iso_year=iso_year,
        iso_week=iso_week,
        monday=monday,
        sunday=monday + timedelta(days=6),
    )


# ---------- 記事収集 ----------

def collect_week_articles(week: WeekRange) -> list["dd.Article"]:
    """週の範囲（月 0:00 〜 日 23:59 JST、mtime基準）に保存された
    ファイルを対象にする。"""
    if not ARTICLES_DIR.exists():
        return []
    start = datetime.combine(week.monday, datetime.min.time())
    end = datetime.combine(week.sunday, datetime.max.time())
    results: list[dd.Article] = []
    for p in sorted(ARTICLES_DIR.glob("*.md")):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime)
        except OSError:
            continue
        if mtime < start or mtime > end:
            continue
        art = dd.parse_article(p)
        if art is not None:
            results.append(art)
    return results


def pick_genre_topN(articles: list["dd.Article"]) -> dict[str, list["dd.Article"]]:
    """ジャンル毎に上位 PER_GENRE_CAP 件まで取り、全体で TOTAL_TOP_ARTICLES 件にトリムする。"""
    by_genre: dict[str, list[dd.Article]] = {g: [] for g in GENRE_ORDER}
    for art in articles:
        g = art.genre if art.genre in by_genre else _fallback_genre(art.genre)
        by_genre.setdefault(g, []).append(art)
    for g in by_genre:
        by_genre[g].sort(key=lambda a: a.score, reverse=True)
        by_genre[g] = by_genre[g][:PER_GENRE_CAP]

    # 合計 TOTAL_TOP_ARTICLES 件に絞る（全体スコア順で下位から削る）
    flat = [(a.score, g, a) for g, arts in by_genre.items() for a in arts]
    flat.sort(key=lambda t: t[0], reverse=True)
    kept = set(id(a) for _, _, a in flat[:TOTAL_TOP_ARTICLES])
    for g in by_genre:
        by_genre[g] = [a for a in by_genre[g] if id(a) in kept]
    return by_genre


def _fallback_genre(g: str) -> str:
    # 既知ジャンル名の別表記を吸収（将来の rss_collector 変更に備える）
    if not g:
        return "その他"
    return g


# ---------- Wiki 概念 ----------

@dataclass
class Concept:
    path: Path
    title: str = ""
    tags: list[str] = field(default_factory=list)
    updated: Optional[date] = None
    description: str = ""
    birth_time: float = 0.0


_FM_RE = dd._FM_RE  # 同じフロントマター正規表現を再利用


def parse_concept(path: Path) -> Concept:
    c = Concept(path=path, title=path.stem)
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return c
    try:
        c.birth_time = path.stat().st_birthtime  # macOS
    except (AttributeError, OSError):
        try:
            c.birth_time = path.stat().st_mtime
        except OSError:
            c.birth_time = 0.0

    m = _FM_RE.match(text)
    body = text
    if m:
        fm_text, body = m.group(1), m.group(2)
        desc_from_fm = ""
        for line in fm_text.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"')
            if key == "title" and val:
                c.title = val
            elif key == "updated" and val:
                try:
                    c.updated = date.fromisoformat(val)
                except ValueError:
                    c.updated = None
            elif key == "description" and val:
                desc_from_fm = val
            elif key == "tags":
                inner = val.strip().strip("[]")
                c.tags = [t.strip() for t in inner.split(",") if t.strip()]
        if desc_from_fm:
            c.description = desc_from_fm

    if not c.description:
        c.description = _extract_first_sentence(body)
    return c


def _extract_first_sentence(body: str, max_len: int = 100) -> str:
    for para in re.split(r"\n\s*\n", body):
        line = para.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = re.sub(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"[*_`>]+", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        # 最初の「。」で切る
        m = re.search(r"[。！？!?]", line)
        if m:
            line = line[: m.end()]
        if len(line) > max_len:
            line = line[:max_len].rstrip() + "…"
        return line
    return "(説明なし)"


def load_all_concepts() -> list[Concept]:
    if not CONCEPTS_DIR.exists():
        return []
    return [parse_concept(p) for p in sorted(CONCEPTS_DIR.glob("*.md"))]


def concepts_this_week(all_concepts: list[Concept], week: WeekRange) -> list[Concept]:
    result = []
    for c in all_concepts:
        if c.updated and week.monday <= c.updated <= week.sunday:
            result.append(c)
    result.sort(key=lambda c: (c.updated or date.min, c.title))
    return result


# ---------- lint ----------

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


@dataclass
class LintResult:
    orphans: list[Concept] = field(default_factory=list)
    broken_links: list[tuple[str, str]] = field(default_factory=list)  # (source_page, bad_target)
    new_unlinked: list[Concept] = field(default_factory=list)


def _resolve_link_target(raw: str) -> str:
    # [[ページ名|表示]] や [[ページ名#見出し]] からページ名部分だけ取る
    target = raw.split("|", 1)[0]
    target = target.split("#", 1)[0]
    return target.strip()


def _collect_all_wiki_pages() -> dict[str, Path]:
    pages: dict[str, Path] = {}
    for p in WIKI_DIR.rglob("*.md"):
        pages[p.stem] = p
    return pages


def run_lint(week: WeekRange, all_concepts: list[Concept]) -> LintResult:
    pages = _collect_all_wiki_pages()
    page_names = set(pages.keys())

    inbound: dict[str, list[str]] = {name: [] for name in page_names}
    broken: list[tuple[str, str]] = []

    for name, path in pages.items():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        seen_local: set[str] = set()
        for raw in _WIKILINK_RE.findall(text):
            target = _resolve_link_target(raw)
            if not target or target == name:
                continue
            if target in seen_local:
                continue
            seen_local.add(target)
            if target in page_names:
                inbound[target].append(name)
            else:
                broken.append((name, target))

    # 孤立ページ: concepts 配下でインバウンド 0 件
    orphans: list[Concept] = []
    for c in all_concepts:
        stem = c.path.stem
        if not inbound.get(stem):
            orphans.append(c)

    # 新規作成 & 未リンク: birth_time が週内 かつ インバウンド 0
    week_start_ts = _date_to_ts(week.monday)
    week_end_ts = _date_to_ts(week.sunday + timedelta(days=1))  # 排他
    new_unlinked: list[Concept] = []
    for c in all_concepts:
        if not (week_start_ts <= c.birth_time < week_end_ts):
            continue
        if not inbound.get(c.path.stem):
            new_unlinked.append(c)

    orphans.sort(key=lambda c: c.title)
    broken.sort()
    new_unlinked.sort(key=lambda c: c.title)
    return LintResult(orphans=orphans, broken_links=broken, new_unlinked=new_unlinked)


def _date_to_ts(d: date) -> float:
    from datetime import datetime as _dt
    return _dt(d.year, d.month, d.day).timestamp()


# ---------- 出力 ----------

def collect_health_log(week: WeekRange) -> dict:
    """rss_collector.log から当週(月〜日)の自動処理の成否を集計する（読むだけ）。
    α成功行「α: N件を意味判定で選定」には日付が無いため、直近の日付付き行
    （[daily-digest] 対象日: や [compile] 行）から日付コンテキストを引き継いで判定する。"""
    health = {
        "ok": False,
        "alpha_success": 0,
        "alpha_warn": 0,
        "warn_breakdown": {"タイムアウト": 0, "非JSON": 0, "例外": 0, "選定ゼロ": 0, "その他": 0},
        "compile_ok": 0,
        "compile_runs": 0,
    }
    if not RSS_LOG.exists():
        return health
    try:
        text = RSS_LOG.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return health

    date_re = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
    cur: Optional[date] = None
    for line in text.splitlines():
        m = date_re.search(line)
        if m:
            try:
                cur = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        if cur is None or not (week.monday <= cur <= week.sunday):
            continue
        if "[compile]" in line and "終了" in line:
            health["compile_runs"] += 1
            if "exit=0" in line:
                health["compile_ok"] += 1
        elif "件を意味判定で選定" in line:
            health["alpha_success"] += 1
        elif "[WARN] α" in line:
            health["alpha_warn"] += 1
            bd = health["warn_breakdown"]
            if "タイムアウト" in line:
                bd["タイムアウト"] += 1
            elif "非JSON" in line or "JSON parse" in line:
                bd["非JSON"] += 1
            elif "例外" in line or "実行失敗" in line:
                bd["例外"] += 1
            elif "選定ゼロ" in line:
                bd["選定ゼロ"] += 1
            else:
                bd["その他"] += 1  # subprocess版の「異常終了(rc=)」等
    health["ok"] = True
    return health


def render_report(
    week: WeekRange,
    today: date,
    by_genre: dict[str, list["dd.Article"]],
    articles_total: int,
    week_concepts: list[Concept],
    ideas: list[dict],
    lint: LintResult,
) -> str:
    lines: list[str] = []
    lines.append("---")
    lines.append(f"date: {today.strftime('%Y-%m-%d')}")
    lines.append(f"week: {week.label}")
    lines.append("type: weekly-report")
    lines.append(f"articles_this_week: {articles_total}")
    lines.append(f"new_concepts: {len(week_concepts)}")
    lines.append("tags: [report, weekly]")
    lines.append("---")
    lines.append("")
    lines.append(f"# ウィークリーレポート {week.iso_year}-{week.label}")
    lines.append(
        f"_対象期間: {week.monday.strftime('%Y-%m-%d')} (月) "
        f"〜 {week.sunday.strftime('%Y-%m-%d')} (日)_"
    )
    lines.append("")

    # 1. トップ記事（ジャンル別）
    lines.append(f"## 1. 今週のトップ記事（ジャンル別・最大{TOTAL_TOP_ARTICLES}件）")
    lines.append("")
    shown = 0
    for genre in GENRE_ORDER:
        arts = by_genre.get(genre, [])
        lines.append(f"### {genre}")
        if not arts:
            lines.append("- （該当なし）")
            lines.append("")
            continue
        for art in arts:
            summary = dd.one_line_summary(art, 80)
            lines.append(f"- **{art.title}** （スコア: {art.score}）")
            lines.append(f"  - URL: {art.source_url}")
            lines.append(f"  - 要約: {summary}")
            shown += 1
        lines.append("")
    # 想定外のジャンル（rss_collector 追加時の保険）
    extras = [g for g in by_genre if g not in GENRE_ORDER and by_genre[g]]
    for genre in extras:
        lines.append(f"### {genre}")
        for art in by_genre[genre]:
            summary = dd.one_line_summary(art, 80)
            lines.append(f"- **{art.title}** （スコア: {art.score}）")
            lines.append(f"  - URL: {art.source_url}")
            lines.append(f"  - 要約: {summary}")
            shown += 1
        lines.append("")

    # 2. 今週追加された Wiki 概念
    lines.append("## 2. 今週追加・更新された Wiki 概念")
    lines.append("")
    if not week_concepts:
        lines.append("_該当なし_")
        lines.append("")
    else:
        for c in week_concepts:
            updated = c.updated.strftime("%Y-%m-%d") if c.updated else "-"
            lines.append(f"- [[{c.path.stem}]] (updated: {updated}) — {c.description}")
        lines.append("")

    # 3. ブログ記事候補
    lines.append("## 3. ブログ記事候補トップ3")
    lines.append("")
    if not ideas:
        lines.append("_候補にできる記事がありません。_")
        lines.append("")
    for i, idea in enumerate(ideas, 1):
        lines.append(f"### ネタ{i}: {idea['title']}")
        lines.append(f"- 想定読者: {idea['readers']}")
        lines.append("- 骨子:")
        for ol in idea["outline"]:
            lines.append(f"  - {ol}")
        if idea.get("source_url"):
            lines.append(f"- 参考記事: {idea['source_url']}")
        lines.append("")

    # 4. lint 結果
    lines.append("## 4. lint 結果（Wiki 健全性）")
    lines.append("")
    lines.append(f"### 4-1. 孤立ページ（inbound リンクなし）: {len(lint.orphans)} 件")
    if not lint.orphans:
        lines.append("- （なし）")
    else:
        for c in lint.orphans:
            lines.append(f"- [[{c.path.stem}]]")
    lines.append("")

    lines.append(f"### 4-2. 壊れたリンク: {len(lint.broken_links)} 件")
    if not lint.broken_links:
        lines.append("- （なし）")
    else:
        for src, bad in lint.broken_links:
            lines.append(f"- [[{src}]] → [[{bad}]] が存在しない")
    lines.append("")

    lines.append(f"### 4-3. 今週新規作成 & 未リンクの概念: {len(lint.new_unlinked)} 件")
    if not lint.new_unlinked:
        lines.append("- （なし）")
    else:
        for c in lint.new_unlinked:
            lines.append(f"- [[{c.path.stem}]] — {c.description}")
    lines.append("")

    # 5. 自動処理 死活状況（rss_collector.log 集計・読むだけ）。集計失敗でも本体は壊さない。
    lines.append("## 5. 自動処理 死活状況（今週）")
    lines.append("")
    try:
        h = collect_health_log(week)
        if not h["ok"]:
            lines.append("- ⚠️ ログ未取得（rss_collector.log が読めず集計不可）")
        else:
            if h["compile_runs"] == 0:
                lines.append("- 🔴 **compile: 今週の実行記録なし**（静かに停止している可能性。要確認）")
            else:
                lines.append(f"- compile（wiki化）: 成功 {h['compile_ok']}/{h['compile_runs']} 回")
            if h["alpha_success"] == 0:
                lines.append(
                    f"- 🟡 **α（SE価値判定）: 今週の成功ゼロ**"
                    f"（β順フォールバックで代替中・失敗 {h['alpha_warn']} 回）"
                )
            else:
                lines.append(
                    f"- α（SE価値判定）: 成功 {h['alpha_success']} 回 / 失敗 {h['alpha_warn']} 回"
                )
            if h["alpha_warn"]:
                detail = " / ".join(f"{k}={v}" for k, v in h["warn_breakdown"].items() if v)
                if detail:
                    lines.append(f"  - α失敗内訳: {detail}")
    except Exception as e:
        lines.append(f"- ⚠️ 死活集計をスキップ（{type(e).__name__}）")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------- メイン ----------

def print_report(
    week: WeekRange,
    articles_total: int,
    by_genre: dict[str, list["dd.Article"]],
    week_concepts: list[Concept],
    ideas: list[dict],
    lint: LintResult,
    out_path: Path,
) -> None:
    print("\n========== 実行ログ ==========")
    print(
        f"[weekly-report] 週={week.iso_year}-{week.label} "
        f"({week.monday}〜{week.sunday}) "
        f"対象記事={articles_total} 概念={len(week_concepts)}"
    )
    print("------------------------------")
    for genre in GENRE_ORDER:
        arts = by_genre.get(genre, [])
        print(f"  [{genre}] {len(arts)} 件")
        for art in arts:
            title_short = art.title if len(art.title) <= 60 else art.title[:60] + "…"
            print(f"    - スコア{art.score:3d}  {title_short}")
    print("------------------------------")
    print(
        f"[weekly-report] lint: 孤立={len(lint.orphans)} "
        f"壊れた={len(lint.broken_links)} 新規未リンク={len(lint.new_unlinked)}"
    )
    print(f"[weekly-report] ブログネタ: {len(ideas)} 件")
    print(f"出力先: {out_path}")


def main() -> int:
    today = date.today()
    week = current_week(today)
    print(f"[weekly-report] today={today} week={week.iso_year}-{week.label}")
    print(f"[weekly-report] 対象期間: {week.monday} 〜 {week.sunday}")

    profile = dd.parse_interest_profile(INTEREST_PROFILE)
    print(
        f"[weekly-report] interest_profile: "
        f"強={len(profile.strong)}語 / 中={len(profile.medium)}語 / "
        f"除外={len(profile.negative)}語"
    )

    articles = collect_week_articles(week)
    print(f"[weekly-report] 当週の新着記事: {len(articles)}件")
    for art in articles:
        dd.score_article(art, profile)

    by_genre = pick_genre_topN(articles)

    # ブログネタは週全体のスコア降順（daily_digest の本文フィルタを流用）
    ideas_source = sorted(articles, key=lambda a: a.score, reverse=True)
    ideas = dd.pick_blog_ideas(ideas_source, n=3)

    all_concepts = load_all_concepts()
    week_concepts = concepts_this_week(all_concepts, week)
    lint = run_lint(week, all_concepts)

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUTS_DIR / f"{week.file_stem}.md"
    out_path.write_text(
        render_report(week, today, by_genre, len(articles), week_concepts, ideas, lint),
        encoding="utf-8",
    )

    print_report(week, len(articles), by_genre, week_concepts, ideas, lint, out_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"[weekly-report] [ERROR] {type(e).__name__}: {e}\n")
        raise
