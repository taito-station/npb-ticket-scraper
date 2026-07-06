"""阪神タイガースのチケット発売スケジュール取得アダプター。

サイト構造（2026 時点、実ページ調査済み）:
- 発見: チケットカテゴリの月別アーカイブ ``/news/topics/ticket/{YYYYMM}`` が静的な記事一覧。
  各記事は ``/news/topics/info_XXXXX.html`` へのリンク＋``[YY/MM/DD]`` 日付＋タイトル。
  カテゴリトップ ``/news/topics/ticket/`` は一覧を JS 描画するため使わず、月別アーカイブを走査する。
- 記事本文: 発売告知は本文段落に散文で記載（球場ごとに 1 段落、各段落に
  「{M月D日(曜)HH:MM}より{チャネル}」形式でネット/店舗の発売日時）。年表記は無く記事日付から推定する。
  個別の対戦カードは列挙されず別ページ（動的）に委譲されるため、#3 PoC では球場単位で扱い games は空にする。

規約確認済み: robots.txt 不在（全許可）・利用規約に自動アクセス禁止条項なし・抽出対象は著作権保護外の
事実（docs/decisions.md §8）。低頻度アクセスの作法を守る。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .. import fetcher
from ..models import SaleSchedule, SaleType, TeamId
from .base import TeamAdapter

JST = ZoneInfo("Asia/Tokyo")
_BASE = "https://hanshintigers.jp"

# 記事詳細ページ（info_XXXXX.html）への相対/絶対リンク。数字部分が安定した記事 ID。
_ARTICLE_HREF_RE = re.compile(r"/news/topics/info_(\d+)\.html")
# 記事一覧行の日付表記 [YY/MM/DD]
_LIST_DATE_RE = re.compile(r"\[(\d{2})/(\d{2})/(\d{2})\]")
# 発売散文中の「M月D日(曜)[HH:MM]より{チャネル}」。時刻は無い告知もあるためオプション。
_SALE_RE = re.compile(
    r"(\d{1,2})月(\d{1,2})日\([月火水木金土日]\)"
    r"(?:\s*(\d{1,2}):(\d{2}))?\s*より\s*(インターネット|各店舗|店舗|電話)"
)

# 球場キーワード → (正式名, source_key 用スラッグ)
_VENUES: tuple[tuple[str, str, str], ...] = (
    ("甲子園", "阪神甲子園球場", "koshien"),
    ("京セラ", "京セラドーム大阪", "kyocera"),
)
# 販売チャネル原文 → スラッグ
_CHANNELS: dict[str, str] = {
    "インターネット": "net",
    "各店舗": "store",
    "店舗": "store",
    "電話": "phone",
}


@dataclass(frozen=True, slots=True)
class ArticleRef:
    """記事一覧から発見した 1 記事への参照。"""

    url: str
    title: str
    published_date: date


def _classify_sale_type(title: str) -> SaleType:
    """記事タイトルから発売区分を推定する。判定順は具体的なものを優先。"""
    if "抽選" in title:
        return SaleType.LOTTERY
    # 「レギュラーシーズン」等の誤検出を避けるため座席種を明示するキーワードで判定する
    if "シーズンシート" in title or "シーズンチケット" in title:
        return SaleType.SEASON_SEAT_PRESALE
    if any(k in title for k in ("ファンクラブ", "会員", "先行")):
        return SaleType.FANCLUB_PRESALE
    if "発売" in title or "一般" in title:
        return SaleType.GENERAL
    return SaleType.OTHER


def _infer_year(month: int, published: date) -> int:
    """年表記なしの発売月に、記事公開日から年を補う。

    発売月が公開月より小さければ翌年ぶんの告知とみなす（年跨ぎの発売告知に対応）。
    """
    year = published.year
    if month < published.month:
        year += 1
    return year


def _venues_in(text: str) -> list[tuple[str, str]]:
    """テキストに登場する球場（正式名, スラッグ）を出現順で返す。

    1 文に「甲子園ならびに京セラ」のように複数球場が並ぶ告知があるため複数対応する。
    """
    return [(name, slug) for keyword, name, slug in _VENUES if keyword in text]


def parse_article_list(html: str, *, base_url: str = _BASE) -> list[ArticleRef]:
    """月別アーカイブ HTML から発売系記事の参照を抽出する。

    タイトルに「発売」「販売」を含む記事のみ対象にする（グッズ告知や注意喚起を除外）。
    """
    soup = BeautifulSoup(html, "html.parser")
    refs: list[ArticleRef] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=_ARTICLE_HREF_RE):
        href = anchor["href"]
        url = href if href.startswith("http") else f"{base_url}{href}"
        if url in seen:
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if "発売" not in title and "販売" not in title:
            continue
        published = _extract_list_date(anchor)
        if published is None:
            continue
        seen.add(url)
        refs.append(ArticleRef(url=url, title=title, published_date=published))
    return refs


def _extract_list_date(anchor: object) -> date | None:
    """記事リンクの周辺行から ``[YY/MM/DD]`` を読む。"""
    node = anchor
    for _ in range(4):  # li/dd/tr など日付を含む最小の親までさかのぼる
        parent = node.find_parent() if hasattr(node, "find_parent") else None
        if parent is None:
            break
        match = _LIST_DATE_RE.search(parent.get_text(" ", strip=True))
        if match:
            yy, mm, dd = (int(g) for g in match.groups())
            return date(2000 + yy, mm, dd)
        node = parent
    return None


def _article_id(url: str) -> str:
    match = _ARTICLE_HREF_RE.search(url)
    if match is None:
        raise ValueError(f"記事 ID を URL から抽出できません: {url}")
    return match.group(1)


def _article_title(soup: BeautifulSoup) -> str:
    title_tag = soup.find("title")
    if title_tag is None:
        return ""
    # 「<記事名>｜球団ニュース｜…」の先頭要素が記事名
    return title_tag.get_text(strip=True).split("｜")[0].strip()


def parse_sale_article(html: str, *, article_url: str, published_date: date) -> list[SaleSchedule]:
    """記事詳細 HTML から球場×チャネル単位の SaleSchedule 群を生成する。

    告知の散文には 2 系統ある: ①球場ごとに別日程（「甲子園…また京セラ…」）、
    ②複数球場に同一日程（「甲子園ならびに京セラ…」）。共通処理として、発売情報ブロックを
    「また」で節に分割し、各節に登場する球場（無ければ前節を継承）へ、その節の全発売
    セグメントを割り当てる。時刻の無い告知は日付のみ保持し notes に要確認と記す。
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find(id="news-entry") or soup
    article_id = _article_id(article_url)
    title = _article_title(soup)
    sale_type = _classify_sale_type(title)
    season_label = "オープン戦" if "オープン戦" in title else "公式戦"

    schedules: list[SaleSchedule] = []
    seen_keys: set[str] = set()
    for block in body.find_all(["p", "td", "li"]):
        text = " ".join(block.get_text(" ", strip=True).split())
        if not _SALE_RE.search(text):
            continue
        current_venues: list[tuple[str, str]] = []
        for clause in text.split("また"):
            venues = _venues_in(clause)
            if venues:
                current_venues = venues
            if not current_venues:
                continue
            for mo, day, hh, mm, channel in _SALE_RE.findall(clause):
                for venue_name, venue_slug in current_venues:
                    source_key = f"hanshin:{article_id}:{venue_slug}:{_CHANNELS[channel]}"
                    if source_key in seen_keys:
                        continue
                    seen_keys.add(source_key)
                    year = _infer_year(int(mo), published_date)
                    if hh:
                        sale_start = datetime(year, int(mo), int(day), int(hh), int(mm), tzinfo=JST)
                        notes = venue_name
                    else:  # 時刻未記載の告知は日付のみ確定し、時刻は人手確認に委ねる
                        sale_start = datetime(year, int(mo), int(day), tzinfo=JST)
                        notes = f"{venue_name}（発売時刻は要確認）"
                    schedules.append(
                        SaleSchedule(
                            selling_team=TeamId.HANSHIN,
                            sale_type=sale_type,
                            sale_label=f"{venue_name} {channel}発売（{season_label}）",
                            sale_start=sale_start,
                            sale_end=None,
                            games=[],
                            official_url=article_url,
                            source_url=article_url,
                            source_key=source_key,
                            membership_rank=None,
                            notes=notes,
                        )
                    )
    return schedules


def _recent_months(today: date, *, back: int = 3) -> list[str]:
    """走査対象の月アーカイブ（YYYYMM）を新しい順で返す（当月＋過去 back ヶ月）。"""
    months: list[str] = []
    year, month = today.year, today.month
    for _ in range(back + 1):
        months.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return months


class HanshinAdapter(TeamAdapter):
    """阪神タイガースの発売スケジュール取得アダプター。"""

    team_id = TeamId.HANSHIN
    TICKET_ARCHIVE_URL = f"{_BASE}/news/topics/ticket/{{year_month}}"

    def __init__(self, fetch: Callable[[str], str] = fetcher.get_text) -> None:
        # fetch を差し替え可能にし、パースをネットワークから切り離してテストする。
        self._fetch = fetch

    def fetch_schedules(self, *, months: list[str] | None = None) -> list[SaleSchedule]:
        # months 未指定なら当月＋直近数ヶ月（本番の日次実行向け）。過去月を渡せば初期投入/デモに使える。
        if months is None:
            months = _recent_months(datetime.now(JST).date())
        refs: dict[str, ArticleRef] = {}
        for year_month in months:
            list_html = self._fetch(self.TICKET_ARCHIVE_URL.format(year_month=year_month))
            for ref in parse_article_list(list_html):
                refs.setdefault(ref.url, ref)  # 月をまたぐ重複を排除

        schedules: list[SaleSchedule] = []
        seen_keys: set[str] = set()
        for ref in refs.values():
            article_html = self._fetch(ref.url)
            for schedule in parse_sale_article(
                article_html, article_url=ref.url, published_date=ref.published_date
            ):
                if schedule.source_key in seen_keys:
                    continue
                seen_keys.add(schedule.source_key)
                schedules.append(schedule)
        return schedules
