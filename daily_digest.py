#!/usr/bin/env python3
"""
Daily Digest Generator

今日 raw/articles/ に保存された記事を interest_profile.md の
キーワードでスコアリングし、トップ5とブログ記事ネタ3件の
ダイジェストを outputs/digest-YYYY-MM-DD.md に出力する。

使い方:
    python3 daily_digest.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ---------- 設定 ----------

HOME = Path.home()
WIKI_ROOT = HOME / "Library/Mobile Documents/iCloud~md~obsidian/Documents/llm-wiki"
ARTICLES_DIR = WIKI_ROOT / "raw" / "articles"
OUTPUTS_DIR = WIKI_ROOT / "outputs"
INTEREST_PROFILE = WIKI_ROOT / "interest_profile.md"

# スコア基準（interest_profile.md の記載と一致させる）
SCORE_STRONG = 30
SCORE_MEDIUM = 15
SCORE_NEGATIVE = -30
BASE_SCORE = 50  # キーワード一致ゼロでも中立として 50 点から出発
SCORE_MAX = 130  # 同点を減らすため上限を 100 → 130 に拡大
MIN_BODY_LEN_FOR_IDEA = 200  # ブログネタ候補にする最低本文長（文字）
DIGEST_WINDOW_HOURS = 24  # 直近 N 時間に保存された記事を「今日の新着」とみなす

# ---------- α（SE価値判定）設定 ----------
ALPHA_BETA_TOP_N = 12         # β足切り：αに渡す上位件数
ALPHA_FINAL_N = 5             # α が選ぶ最終件数
ALPHA_SCRIPT = HOME / "my-scripts" / "score_se_value.sh"
ALPHA_BODY_CHARS = 300        # 各記事の本文をプロンプトに載せる際の冒頭文字数
ALPHA_TIMEOUT_SEC = 180       # score_se_value.sh のタイムアウト


# ---------- データ ----------

@dataclass
class Article:
    path: Path
    title: str = ""
    source_url: str = ""
    date_clipped: str = ""
    genre: str = ""
    tags: list[str] = field(default_factory=list)
    body: str = ""
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    se_rank: int = 0          # α が付けた SE価値ランク（1が最良、0は未判定）
    se_reason: str = ""       # α が付けた判定理由


@dataclass
class InterestProfile:
    strong: list[str] = field(default_factory=list)
    medium: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)


# ---------- I/O ヘルパー ----------

def _read_text_with_retry(
    path: Path,
    retries: int = 3,
    wait: float = 2.0,
    encoding: str = "utf-8",
    errors: Optional[str] = None,
) -> str:
    """
    iCloud Drive 上のファイルは他プロセス（compile_wiki.sh や iCloud 同期デーモン）
    とロック競合（EDEADLK）を起こすことがあるため、Errno 11 の場合のみ wait 秒待って
    リトライする。それ以外の OSError は即座に raise。
    """
    for i in range(retries):
        try:
            if errors is not None:
                return path.read_text(encoding=encoding, errors=errors)
            return path.read_text(encoding=encoding)
        except OSError as e:
            if e.errno == 11 and i < retries - 1:  # EDEADLK
                print(
                    f"[daily-digest] [WARN] {path.name} ロック競合、"
                    f"{wait}s後にretry ({i+1}/{retries})",
                    flush=True,
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


# ---------- パース ----------

_FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def parse_article(path: Path) -> Optional[Article]:
    try:
        text = _read_text_with_retry(path, errors="ignore")
    except OSError:
        return None
    m = _FM_RE.match(text)
    if not m:
        return None
    fm_text, body = m.group(1), m.group(2)
    art = Article(path=path, body=body)
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"')
        if key == "title":
            art.title = val
        elif key == "source_url":
            art.source_url = val
        elif key == "date_clipped":
            art.date_clipped = val
        elif key == "genre":
            art.genre = val
        elif key == "tags":
            inner = val.strip().strip("[]")
            art.tags = [t.strip() for t in inner.split(",") if t.strip()]
    return art


_KW_SPLIT = re.compile(r"[・、,/()（）\s]+")


def parse_interest_profile(path: Path) -> InterestProfile:
    profile = InterestProfile()
    if not path.exists():
        return profile
    current: Optional[list[str]] = None
    for line in _read_text_with_retry(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            if "強く興味" in stripped:
                current = profile.strong
            elif "読まなくていい" in stripped or "興味なし" in stripped:
                current = profile.negative
            elif "興味あり" in stripped:
                current = profile.medium
            else:
                current = None
            continue
        if current is None:
            continue
        if not stripped.startswith("-"):
            continue
        content = stripped.lstrip("-").strip()
        # 行単位のフレーズをトークンに分解してキーワードとして登録
        for tok in _KW_SPLIT.split(content):
            tok = tok.strip()
            if len(tok) >= 2:
                current.append(tok)
    # 重複除去（出現順維持）
    profile.strong = list(dict.fromkeys(profile.strong))
    profile.medium = list(dict.fromkeys(profile.medium))
    profile.negative = list(dict.fromkeys(profile.negative))
    return profile


# ---------- スコアリング ----------

def score_article(art: Article, profile: InterestProfile) -> None:
    text = f"{art.title}\n{' '.join(art.tags)}\n{art.genre}\n{art.body}"
    score = BASE_SCORE
    reasons: list[str] = []
    seen: set[str] = set()

    def apply(keywords: list[str], delta: int, label: str) -> None:
        nonlocal score
        for kw in keywords:
            if kw in seen:
                continue
            if kw in text:
                score += delta
                sign = "+" if delta > 0 else ""
                reasons.append(f"{sign}{delta}「{kw}」({label})")
                seen.add(kw)

    # 強い興味 → 興味あり → 除外 の順で一致を記録
    apply(profile.strong, SCORE_STRONG, "強い興味")
    apply(profile.medium, SCORE_MEDIUM, "興味あり")
    apply(profile.negative, SCORE_NEGATIVE, "興味薄")

    art.score = max(0, min(SCORE_MAX, score))
    art.score_reasons = reasons


# ---------- α（SE価値意味判定） ----------

def build_alpha_prompt(articles: list["Article"]) -> str:
    """β通過記事から α判定用プロンプトを組み立てる。
    記事番号(id)は 1始まりで、articles のインデックス+1 に対応させる。"""
    header = (
        "あなたは「SE→AI活用開発に移行しようとしている技術者」向けの情報"
        "キュレーターである。以下の記事リストを、その読者にとっての価値で評価し、"
        "上位5本を選んで順位づけせよ。\n\n"
        "# 評価軸（この順で重視する）\n"
        "1. 実装可能性: ソロのエンジニアが今週、自分の手で試せる具体性があるか。"
        "概念論・ポエム・宣伝は低評価。\n"
        "2. 近道性: SE→AI活用開発の移行で、数か月分の試行錯誤を省ける情報か。"
        "「やり方の一般論」でなく「動く成果・具体的手順」を高評価。\n"
        "3. 陳腐化耐性: 半年後も価値が残るか。流行先行・一過性の話題は減点。\n"
        "4. 着手喚起力: 上記1〜2を満たす記事に限り、読者が「自分も今日試そう」と"
        "具体的な次の一歩を描けるものを加点。※単に気分を高揚させるだけ・"
        "危機感を煽るだけの記事は加点しない。\n\n"
        "# 出力形式（厳守）\n"
        "JSON配列のみを出力する。前後に説明文・コードブロック記号は一切付けない。"
        "各要素は以下のキーを持つ:\n"
        '  "id": 入力で与えた記事番号（整数）\n'
        '  "rank": 1〜5の順位（1が最良）\n'
        '  "reason": なぜこの順位か、移行期SE視点で40字以内の日本語\n'
        "上位5本のみ。6本目以降は出力しない。\n\n"
        "# 入力記事\n"
    )
    blocks = []
    for i, art in enumerate(articles, 1):
        body_head = (art.body or "").strip().replace("\n", " ")[:ALPHA_BODY_CHARS]
        blocks.append(f"[{i}] タイトル: {art.title}\n冒頭: {body_head}")
    return header + "\n\n".join(blocks) + "\n"


def rank_by_se_value(beta_top: list["Article"]) -> list["Article"]:
    """β通過記事を score_se_value.sh に渡し、α判定で上位を選定して返す。
    失敗時は β スコア順の上位 ALPHA_FINAL_N 件をそのまま返す（フォールバック）。"""
    fallback = beta_top[:ALPHA_FINAL_N]
    if not beta_top:
        return fallback
    if not ALPHA_SCRIPT.exists():
        print(f"[daily-digest] [WARN] α: {ALPHA_SCRIPT} が無い → β順で代替", flush=True)
        return fallback

    prompt = build_alpha_prompt(beta_top)
    try:
        proc = subprocess.run(
            [str(ALPHA_SCRIPT)],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=ALPHA_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print("[daily-digest] [WARN] α: タイムアウト → β順で代替", flush=True)
        return fallback
    except Exception as e:
        print(f"[daily-digest] [WARN] α: 実行失敗({e}) → β順で代替", flush=True)
        return fallback

    if proc.returncode != 0:
        print(f"[daily-digest] [WARN] α: 異常終了(rc={proc.returncode}) → β順で代替", flush=True)
        return fallback

    raw = (proc.stdout or "").strip()
    # 念のため、前後に紛れた説明文があっても最初の [ から最後の ] までを拾う
    s, e = raw.find("["), raw.rfind("]")
    if s != -1 and e != -1 and e > s:
        raw = raw[s : e + 1]
    try:
        ranking = json.loads(raw)
    except Exception as ex:
        print(f"[daily-digest] [WARN] α: JSON parse 失敗({ex}) → β順で代替", flush=True)
        return fallback

    # id(1始まり) → Article に rank/reason を反映
    selected: list[Article] = []
    for item in sorted(ranking, key=lambda r: r.get("rank", 999)):
        idx = item.get("id", 0) - 1
        if 0 <= idx < len(beta_top):
            art = beta_top[idx]
            art.se_rank = item.get("rank", 0)
            art.se_reason = str(item.get("reason", "")).strip()
            selected.append(art)

    if not selected:
        print("[daily-digest] [WARN] α: 有効な選定ゼロ → β順で代替", flush=True)
        return fallback

    print(f"[daily-digest] α: {len(selected)}件を意味判定で選定", flush=True)
    return selected[:ALPHA_FINAL_N]


# ---------- 要約 ----------

def one_line_summary(art: Article, max_len: int = 80) -> str:
    for para in re.split(r"\n\s*\n", art.body):
        clean = para.strip()
        if not clean or clean.startswith("#"):
            continue
        # markdown 装飾を最低限除去
        clean = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", clean)
        clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
        clean = re.sub(r"[*_`>]+", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            continue
        if len(clean) > max_len:
            clean = clean[:max_len].rstrip() + "…"
        return clean
    return "(要約なし)"


# ---------- ブログネタ生成 ----------

_READERS_BY_GENRE = {
    "生成AI・LLM最新動向": "生成AIを実務に組み込みたいエンジニア",
    "エンジニア副業・収益化": "副業で月5〜20万円を目指すエンジニア",
    "金融リテラシー・投資": "投資初心者のITワーカー",
}


def build_idea(art: Article) -> dict:
    genre = art.genre or "技術"
    readers = _READERS_BY_GENRE.get(genre, "スキルアップ志向のエンジニア")
    summary = one_line_summary(art, 100)
    idea_title = f"「{art.title}」を題材に、{genre}の観点で実践ノートを書く"
    outline = [
        f"① 元記事の主張を1分で要約: {summary}",
        "② 自分ならどう使う / 実際に試したコードや手順",
        f"③ 読者（{readers}）への持ち帰りポイント3つ",
    ]
    return {
        "title": idea_title,
        "readers": readers,
        "outline": outline,
        "source_url": art.source_url,
        "source_path": str(art.path),
    }


def _has_enough_body(art: Article) -> bool:
    # markdown 装飾・空白を除いた実質文字数で判定
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", art.body)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[#*_`>\-\s]+", "", text)
    return len(text) >= MIN_BODY_LEN_FOR_IDEA


def pick_blog_ideas(articles: list[Article], n: int = 3) -> list[dict]:
    if not articles:
        return []
    # 本文が十分ある記事のみ候補にする
    candidates = [a for a in articles if _has_enough_body(a)]
    if not candidates:
        return []
    ideas: list[dict] = []
    used_paths: set[str] = set()
    used_genres: set[str] = set()
    # 1 周目: ジャンル分散を優先
    for art in candidates:
        if len(ideas) >= n:
            break
        if art.genre and art.genre in used_genres:
            continue
        ideas.append(build_idea(art))
        used_paths.add(str(art.path))
        if art.genre:
            used_genres.add(art.genre)
    # 2 周目: 足りない分を補充
    for art in candidates:
        if len(ideas) >= n:
            break
        if str(art.path) in used_paths:
            continue
        ideas.append(build_idea(art))
        used_paths.add(str(art.path))
    return ideas


# ---------- ネタシート用テンプレート ----------

MARU_SUJI = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]

PERSONAS_BY_GENRE: dict[str, dict[str, str]] = {
    "生成AI・LLM最新動向": {
        "job": "Webエンジニア3〜8年目、Claude Code / LLM を日常業務に組み込みたい実務家",
        "pain": "AIで時短したいが具体的な手順・ルールが定まらず、試行錯誤だけが増えている",
        "motivation": "再現性のある実装ワークフローを学び、1人開発の生産性を一段引き上げたい",
    },
    "エンジニア副業・収益化": {
        "job": "本業エンジニア、週5〜10時間で副業を回したい会社員",
        "pain": "スキルはあるが単価の上げ方・営業導線が分からず、副業収益が月1〜3万円で頭打ち",
        "motivation": "月5〜20万円の副収入ルートを作り、キャリアの選択肢を広げたい",
    },
    "金融リテラシー・投資": {
        "job": "IT系ワーカー、投資を始めたばかり or 検討中",
        "pain": "NISA・確定申告・ポートフォリオ理論の基礎が体系化できておらず判断に迷う",
        "motivation": "数式と実装の両面で腹落ちさせ、投資判断を自分の言葉で説明できるようになりたい",
    },
}

PERSONA_DEFAULT = {
    "job": "スキルアップを志向するITエンジニア",
    "pain": "断片的な記事は読むが、自分の環境に落とし込めないまま時間が過ぎている",
    "motivation": "体系的に整理された実装手順で、すぐに手を動かせる状態になりたい",
}

OUTLINES_BY_GENRE: dict[str, list[str]] = {
    "生成AI・LLM最新動向": [
        "なぜこのテーマを取り上げるのか（問題意識と背景）",
        "元記事の主張を3行で整理",
        "自分の環境での再現手順（コマンド・設定・ハマりどころ）",
        "つまずきやすい3ポイントと回避策",
        "明日から使える3つのテイクアウェイ",
    ],
    "エンジニア副業・収益化": [
        "著者の現状とゴール（年300万副業）の再確認",
        "元記事の収益化プロセスを分解・フレーム化",
        "自分のスキル棚卸し・時間単価への当てはめ",
        "初月アクションと想定リスク",
        "3ヶ月後のチェックリスト",
    ],
    "金融リテラシー・投資": [
        "概念の教科書的定義を1分で整理",
        "数式・シミュレーションで直感に落とす",
        "Python または スプレッドシートでの再現手順",
        "自分のポートフォリオへの適用アイデア",
        "よくある誤解と次に読むべき参考書",
    ],
}

OUTLINE_DEFAULT = [
    "背景と課題意識（なぜ今このテーマか）",
    "元記事の要点整理（3行サマリ）",
    "自分の環境での再現・検証",
    "つまずきポイントと対処",
    "実践アクション3つ",
]

PAIN_HOOK_BY_GENRE = {
    "生成AI・LLM最新動向": "Claude Codeに時間を溶かしている",
    "エンジニア副業・収益化": "副業の単価が上がらない",
    "金融リテラシー・投資": "投資が怖くて始められない",
}


# ---------- ネタシート生成 ----------

_WEEKLY_FNAME_RE = re.compile(r"^weekly-(\d{4})-W(\d{2})\.md$")
_WEEKLY_IDEA_BLOCK_RE = re.compile(
    r"^### ネタ\d+[:：]\s*(.+?)\n(.*?)(?=\n### |\n## |\Z)",
    re.DOTALL | re.MULTILINE,
)
_WEEKLY_SOURCE_URL_RE = re.compile(
    r"^- (?:参考元記事|参考記事)[:：]\s*(\S+)", re.MULTILINE
)


def _find_latest_weekly_report(outputs_dir: Path) -> Optional[Path]:
    if not outputs_dir.exists():
        return None
    best: Optional[tuple[tuple[int, int], Path]] = None
    for p in outputs_dir.glob("weekly-*.md"):
        m = _WEEKLY_FNAME_RE.match(p.name)
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2)))
        if best is None or key > best[0]:
            best = (key, p)
    return best[1] if best else None


def _parse_weekly_ideas(path: Path) -> list[dict]:
    """weekly レポートから ネタ情報 [{title, source_url}] を順に取り出す。"""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    # 「ブログ記事候補」セクション以降だけを対象にする
    cut = re.search(r"##\s*3\.\s*ブログ記事候補", text)
    scope = text[cut.start():] if cut else text
    results = []
    for m in _WEEKLY_IDEA_BLOCK_RE.finditer(scope):
        title_line = m.group(1).strip()
        block = m.group(2)
        url_m = _WEEKLY_SOURCE_URL_RE.search(block)
        url = url_m.group(1).strip() if url_m else ""
        results.append({"title": title_line, "source_url": url})
    return results


def _build_url_index(articles_dir: Path) -> dict[str, Path]:
    idx: dict[str, Path] = {}
    if not articles_dir.exists():
        return idx
    url_re = re.compile(r'^source_url:\s*"?([^"\n]+?)"?\s*$', re.MULTILINE)
    for p in articles_dir.glob("*.md"):
        try:
            head = p.read_text(encoding="utf-8", errors="ignore")[:2000]
        except OSError:
            continue
        m = url_re.search(head)
        if m:
            idx[m.group(1).strip()] = p
    return idx


_SEO_STRIP_EDGE_RE = re.compile(
    r"^[\s#★☆◎●○■□▲△▼▽※!！\-:：「」『』()（）、。,.]+"
    r"|[\s#★☆◎●○■□▲△▼▽※!！\-:：「」『』()（）、。,.]+$"
)


def _clean_seo_token(t: str) -> str:
    return _SEO_STRIP_EDGE_RE.sub("", t).strip()


def _seo_keywords(
    art: Article, profile: InterestProfile
) -> tuple[str, list[str]]:
    pool: list[str] = []

    def add(x: str, max_len: int = 12) -> None:
        x = _clean_seo_token(x)
        # 全角数字 or 半角数字のみは除外
        if not x or x.isdigit() or len(x) < 2 or len(x) > max_len:
            return
        if x in pool:
            return
        pool.append(x)

    # タグ（編集済みなので少し長めまで許容）
    for t in art.tags:
        add(t, max_len=20)
    # 強い興味 / 中興味のマッチ語（interest_profile の語彙）
    text = art.title + "\n" + art.body
    for kw in profile.strong:
        if kw in text:
            add(kw, max_len=20)
    for kw in profile.medium:
        if kw in text:
            add(kw, max_len=20)
    # タイトル分解（短めだけ採用）
    for tok in _KW_SPLIT.split(art.title):
        add(tok, max_len=12)
    # ジャンル
    if art.genre:
        add(art.genre, max_len=20)
    if not pool:
        return (art.genre or "技術記事", [])
    return (pool[0], pool[1:6])


def _short_topic(title: str, max_len: int = 28) -> str:
    """タイトルから装飾語を落として話題名を短く取り出す。"""
    t = title.strip()
    t = re.sub(r"^[\s#★☆◎●○■□▲△▼▽※!！]+", "", t)
    for suffix in [
        "してみた", "してみる", "してみました", "の話", "のメモ", "の記録",
        "まとめ", "ガイド", "入門",
    ]:
        if t.endswith(suffix) and len(t) > len(suffix) + 2:
            t = t[: -len(suffix)]
    if len(t) > max_len:
        t = t[:max_len].rstrip() + "…"
    return t


def _title_patterns(art: Article, profile: InterestProfile) -> dict[str, str]:
    topic = _short_topic(art.title, 26)
    pain = PAIN_HOOK_BY_GENRE.get(art.genre, "学びが散らかってしまう")
    main_kw, _ = _seo_keywords(art, profile)
    return {
        "A": f"{topic} を実践する 5 つのステップ —— {main_kw} で時短する手順",
        "B": f"{pain} エンジニアへ —— {topic} が突破口になる理由",
        "C": f"{topic} 完全ガイド：{main_kw} の始め方から運用まで",
    }


def _outline_5(art: Article) -> list[str]:
    return list(OUTLINES_BY_GENRE.get(art.genre, OUTLINE_DEFAULT))


def _persona(art: Article) -> dict:
    return dict(PERSONAS_BY_GENRE.get(art.genre, PERSONA_DEFAULT))


def build_idea_sheet(
    art: Article,
    profile: InterestProfile,
    original_title: str,
) -> dict:
    titles = _title_patterns(art, profile)
    main_kw, sub_kws = _seo_keywords(art, profile)
    return {
        "original_title": original_title,
        "title_A": titles["A"],
        "title_B": titles["B"],
        "title_C": titles["C"],
        "persona": _persona(art),
        "outline": _outline_5(art),
        "main_kw": main_kw,
        "sub_kws": sub_kws,
        "source_url": art.source_url,
    }


def gather_idea_sheets(
    today_articles: list[Article],
    profile: InterestProfile,
    articles_dir: Path,
    outputs_dir: Path,
) -> tuple[list[dict], str]:
    """Returns (sheets, mode). mode は 'weekly:<fname>' か 'fallback' か 'none'。"""
    wr_path = _find_latest_weekly_report(outputs_dir)
    if wr_path:
        entries = _parse_weekly_ideas(wr_path)
        if entries:
            url_index = _build_url_index(articles_dir)
            sheets: list[dict] = []
            for e in entries[:3]:
                path = url_index.get(e["source_url"])
                art = parse_article(path) if path else None
                if art is None:
                    continue
                score_article(art, profile)
                sheets.append(build_idea_sheet(art, profile, e["title"]))
            if sheets:
                return sheets, f"weekly:{wr_path.name}"
    # フォールバック: 当日記事の上位3件（本文 200 文字以上）
    scored = sorted(today_articles, key=lambda a: a.score, reverse=True)
    picks = [a for a in scored if _has_enough_body(a)][:3]
    sheets = []
    for a in picks:
        orig = f"「{a.title}」を題材に、{a.genre or '技術'}の観点で実践ノートを書く"
        sheets.append(build_idea_sheet(a, profile, orig))
    return sheets, "fallback" if sheets else "none"


def _render_idea_sheets(sheets: list[dict], mode: str) -> list[str]:
    lines: list[str] = []
    lines.append("## 今週のブログネタシート")
    if mode.startswith("weekly:"):
        fname = mode.split(":", 1)[1]
        lines.append(f"_出典: {fname}_")
    elif mode == "fallback":
        lines.append("_（週次レポート未作成のため、本日のトップ3で代替生成）_")
    else:
        lines.append("_（ネタに展開できる記事が見つかりませんでした）_")
    lines.append("")
    for i, s in enumerate(sheets):
        num = MARU_SUJI[i] if i < len(MARU_SUJI) else str(i + 1)
        lines.append(f"### ネタ{num}：{s['original_title']}")
        lines.append("")
        lines.append("**タイトル案（3パターン）**")
        lines.append(f"- パターンA（数字訴求型）: {s['title_A']}")
        lines.append(f"- パターンB（問題解決型）: {s['title_B']}")
        lines.append(f"- パターンC（ノウハウ型）: {s['title_C']}")
        lines.append("")
        lines.append("**想定読者・ペルソナ**")
        lines.append(f"- 職業・状況: {s['persona']['job']}")
        lines.append(f"- 悩み・課題: {s['persona']['pain']}")
        lines.append(f"- このブログを読む動機: {s['persona']['motivation']}")
        lines.append("")
        lines.append("**記事の骨子（5見出し）**")
        for j, h in enumerate(s["outline"], 1):
            lines.append(f"{j}. {h}")
        lines.append("")
        lines.append("**SEOキーワード候補**")
        lines.append(f"- メインKW: {s['main_kw']}")
        subs = " / ".join(s["sub_kws"]) if s["sub_kws"] else "（候補不足）"
        lines.append(f"- サブKW: {subs}")
        lines.append("")
        lines.append("**参考元記事**")
        lines.append(f"- {s['source_url'] or '(不明)'}")
        lines.append("")
    return lines


# ---------- 出力 ----------

def render_digest(
    today: str,
    top_articles: list[Article],
    ideas: list[dict],
    total_scored: int,
    idea_sheets: Optional[list[dict]] = None,
    sheet_mode: str = "",
) -> str:
    lines: list[str] = []
    lines.append("---")
    lines.append(f"date: {today}")
    lines.append("type: daily-digest")
    lines.append(f"articles_scored: {total_scored}")
    lines.append("tags: [digest, daily]")
    lines.append("---")
    lines.append("")
    lines.append(f"# デイリーダイジェスト {today}")
    lines.append("")
    lines.append("## 今日の新着記事トップ5")
    lines.append("")
    if not top_articles:
        lines.append("_本日 raw/articles/ に新着記事はありません。_")
        lines.append("")
    for i, art in enumerate(top_articles, 1):
        summary = one_line_summary(art, 80)
        lines.append(f"### {i}. {art.title}")
        lines.append(f"- URL: {art.source_url}")
        if art.genre:
            lines.append(f"- ジャンル: {art.genre}")
        lines.append(f"- 要約: {summary}")
        if art.se_reason:
            lines.append(f"- SE価値: {art.se_reason}")
        else:
            # α が使えなかった場合のフォールバック表示（従来のキーワード理由）
            kw = " / ".join(art.score_reasons) if art.score_reasons else "（基礎点のみ）"
            lines.append(f"- スコア理由（β）: {kw}")
        lines.append("")

    lines.append("## ブログ記事ネタの候補（3件）")
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
            lines.append(f"- 参考URL: {idea['source_url']}")
        lines.append("")

    if idea_sheets is not None:
        lines.extend(_render_idea_sheets(idea_sheets, sheet_mode))

    return "\n".join(lines).rstrip() + "\n"


# ---------- メイン ----------

def collect_today_articles(articles_dir: Path, today: str) -> list[Article]:
    """直近 DIGEST_WINDOW_HOURS に保存されたファイルを対象にする。
    ファイル名の日付プレフィックスは記事公開日(UTC)であり、実行日(JST)
    とはズレるため、ここでは mtime で判定する。
    today 引数は出力ファイル名と frontmatter 用にのみ使用する。"""
    if not articles_dir.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=DIGEST_WINDOW_HOURS)
    results: list[Article] = []
    for p in sorted(articles_dir.glob("*.md")):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                continue
        except OSError:
            continue
        art = parse_article(p)
        if art is not None:
            results.append(art)
    return results


def print_report(
    today: str,
    total: int,
    top: list[Article],
    ideas: list[dict],
    out_path: Path,
) -> None:
    print("\n========== 実行ログ ==========")
    print(f"[daily-digest] 対象日={today} 対象記事={total} "
          f"トップ5採用={len(top)} ネタ={len(ideas)}")
    if top:
        print("------------------------------")
        for i, art in enumerate(top, 1):
            title_short = art.title if len(art.title) <= 60 else art.title[:60] + "…"
            print(f"  {i}. スコア{art.score:3d}  {title_short}")
    print("------------------------------")
    print(f"出力先: {out_path}")


def main() -> int:
    today = date.today().strftime("%Y-%m-%d")
    print(f"[daily-digest] 対象日: {today}")
    print(f"[daily-digest] articles_dir: {ARTICLES_DIR}")

    profile = parse_interest_profile(INTEREST_PROFILE)
    print(
        f"[daily-digest] interest_profile: "
        f"強={len(profile.strong)}語 / 中={len(profile.medium)}語 / "
        f"除外={len(profile.negative)}語"
    )

    articles = collect_today_articles(ARTICLES_DIR, today)
    print(f"[daily-digest] 本日の新着記事: {len(articles)}件")

    for art in articles:
        score_article(art, profile)

    articles_sorted = sorted(articles, key=lambda a: a.score, reverse=True)
    beta_top = articles_sorted[:ALPHA_BETA_TOP_N]   # β足切り（上位12）
    top5 = rank_by_se_value(beta_top)               # α判定（失敗時はβ順上位5）
    ideas = pick_blog_ideas(articles_sorted, n=3)

    sheets, sheet_mode = gather_idea_sheets(
        articles, profile, ARTICLES_DIR, OUTPUTS_DIR
    )
    print(f"[daily-digest] ネタシート: {len(sheets)}件 (mode={sheet_mode})")

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUTS_DIR / f"digest-{today}.md"
    out_path.write_text(
        render_digest(today, top5, ideas, len(articles), sheets, sheet_mode),
        encoding="utf-8",
    )

    print_report(today, len(articles), top5, ideas, out_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        sys.stderr.write(f"[daily-digest] [ERROR] {type(e).__name__}: {e}\n")
        raise
