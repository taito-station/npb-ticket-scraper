"""阪神タイガースのチケット発売スケジュール取得アダプター。

サイト構造（2026 時点、実ページ調査済み）:
- 発見: チケットカテゴリの月別アーカイブ ``/news/topics/ticket/{YYYYMM}`` が静的な記事一覧。
  各記事は ``/news/topics/info_XXXXX.html`` へのリンク＋``[YY/MM/DD]`` 日付＋タイトル。
  カテゴリトップ ``/news/topics/ticket/`` は一覧を JS 描画するため使わず、月別アーカイブを走査する。
- 記事本文: 発売告知は本文段落に散文で記載（球場ごとに 1 段落、各段落に
  「{M月D日(曜)HH:MM}より{チャネル}」形式でネット/店舗の発売日時）。年表記は無く記事日付から推定する。
  個別の対戦カードは列挙されず別ページ（動的）に委譲されるため、#3 PoC では球場単位で扱い games は空にする。

規約確認済み: robots.txt 不在（全許可）・利用規約に自動アクセス禁止条項なし・抽出対象は著作権保護外の
事実（docs/decisions.md §8）。低頻度アクセスの作法を守る（実行内でも連続取得の間に小休止を挟む）。
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from .. import fetcher
from ..models import SaleSchedule, SaleType, TeamId
from .base import TeamAdapter

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
_BASE = "https://hanshintigers.jp"
_HOST = urlparse(_BASE).netloc  # 抽出 URL をこのホストに限定する（外部 URL 混入を弾く）

# 記事詳細ページ（info_XXXXX.html）への相対/絶対リンク。数字部分が安定した記事 ID。
_ARTICLE_HREF_RE = re.compile(r"/news/topics/info_(\d+)\.html")
# 記事一覧行の日付表記 [YY/MM/DD]
_LIST_DATE_RE = re.compile(r"\[(\d{2})/(\d{2})/(\d{2})\]")
# 発売散文中の「M月D日[(曜)][HH:MM]より{チャネル}」。曜日括弧・時刻はいずれも無い告知が
# あるためオプション。曜日括弧は「(水・祝)」のような併記も許容する。全角表記は NFKC で半角に寄せる。
_SALE_RE = re.compile(
    r"(\d{1,2})月(\d{1,2})日(?:\([月火水木金土日][^)]{0,4}\))?"
    r"(?:\s*(\d{1,2}):(\d{2}))?\s*より\s*(インターネット|各店舗|店舗|電話)"
)

# 球場キーワード → (正式名, source_key 用スラッグ)
_VENUES: tuple[tuple[str, str, str], ...] = (
    ("甲子園", "阪神甲子園球場", "koshien"),
    ("京セラ", "京セラドーム大阪", "kyocera"),
)
_VENUE_RE = re.compile("|".join(re.escape(keyword) for keyword, _, _ in _VENUES))
_VENUE_BY_KEYWORD = {keyword: (name, slug) for keyword, name, slug in _VENUES}
# 「また」を節境界として分割する。ただし「または」「またがる」「またぐ」など語中の「また」は
# 節境界ではないので分割しない（後続が は/が/ぐ のものを除外）。
_CLAUSE_SPLIT_RE = re.compile(r"また(?![はがぐ])")
# 販売チャネル原文 → スラッグ
_CHANNELS: dict[str, str] = {
    "インターネット": "net",
    "各店舗": "store",
    "店舗": "store",
    "電話": "phone",
}

# 記事一覧で拾う発売系記事のタイトル語彙。発売/販売に加え、抽選・先行・受付の告知も対象にする
# （これらは「発売」を含まないことがあり、含めないと LOTTERY/FANCLUB_PRESALE がクロールで拾えない）。
_SALE_TITLE_KEYWORDS = ("発売", "販売", "抽選", "先行", "受付")

# 同一ホストへ連続アクセスする際に挟む小休止（秒）。礼儀としての最低限のレート制御。
_COURTESY_DELAY = 1.0


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


def parse_article_list(html: str, *, base_url: str = _BASE) -> list[ArticleRef]:
    """月別アーカイブ HTML から発売系記事の参照を抽出する。

    タイトルに「発売」「販売」を含む記事のみ対象にする（グッズ告知や注意喚起を除外）。
    取得先は阪神サイトに限定し、外部ホストの絶対 URL が混入しても弾く。
    """
    soup = BeautifulSoup(html, "html.parser")
    refs: list[ArticleRef] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=_ARTICLE_HREF_RE):
        href = anchor["href"]
        url = href if href.startswith("http") else f"{base_url}{href}"
        if urlparse(url).netloc != _HOST:  # 外部ホストは取得しない（SSRF 面の防御）
            continue
        if url in seen:
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not any(keyword in title for keyword in _SALE_TITLE_KEYWORDS):
            continue
        published = _extract_list_date(anchor)
        if published is None:
            continue
        seen.add(url)
        refs.append(ArticleRef(url=url, title=title, published_date=published))
    return refs


def _extract_list_date(anchor: Tag) -> date | None:
    """記事リンクの周辺行から ``[YY/MM/DD]`` を読む。"""
    node: Tag | None = anchor
    for _ in range(4):  # li/dd/tr など日付を含む最小の親までさかのぼる
        node = node.find_parent() if node is not None else None
        if node is None:
            break
        match = _LIST_DATE_RE.search(node.get_text(" ", strip=True))
        if match:
            yy, mm, dd = (int(g) for g in match.groups())
            return date(2000 + yy, mm, dd)
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


def _assign_clause(
    clause: str, segments: list[re.Match[str]], current: list[tuple[str, str]]
) -> tuple[list[tuple[re.Match[str], list[tuple[str, str]]]], list[tuple[str, str]]]:
    """1 節内の各発売セグメントに球場群を割り当て、更新後の「現在の球場群」を返す。

    節内の球場キーワードとセグメントを出現位置順に走査する。セグメント直前に連続して現れた
    球場（「甲子園ならびに京セラ」なら両方）でその節の球場群を更新し、球場が現れなければ節を
    またいで直前の球場群（``current``）を引き継ぐ（「…発売、また2月2日より…」で日程だけ増える
    ケースに対応）。呼び出し側が節ごとに ``current`` を渡し継ぐことで、「甲子園…また京セラ…」の
    球場別日程も、球場が単独で登場して発売セグメントを持たない節（他球場へ誘導）も正しく扱える。
    """
    events: list[tuple[int, str, object]] = []
    for m in _VENUE_RE.finditer(clause):
        events.append((m.start(), "venue", _VENUE_BY_KEYWORD[m.group()]))
    for seg in segments:
        events.append((seg.start(), "seg", seg))
    events.sort(key=lambda e: e[0])

    assigned: list[tuple[re.Match[str], list[tuple[str, str]]]] = []
    pending: list[tuple[str, str]] = []
    for _pos, kind, payload in events:
        if kind == "venue":
            pending.append(payload)  # type: ignore[arg-type]
        else:
            if pending:  # 直前に現れた球場群で更新（無ければ前節の球場を継承）
                current = list(dict.fromkeys(pending))
                pending = []
            assigned.append((payload, list(current)))  # type: ignore[arg-type]
    if pending:  # セグメントを伴わず球場だけ登場した節は、その球場群を次節へ引き継ぐ
        current = list(dict.fromkeys(pending))
    return assigned, current


def parse_sale_article(html: str, *, article_url: str, published_date: date) -> list[SaleSchedule]:
    """記事詳細 HTML から球場×チャネル単位の SaleSchedule 群を生成する。

    発売情報ブロックを「また」で節に分割し、各節を ``_assign_clause`` に渡して発売セグメント
    （日付・時刻・チャネル）へ球場を割り当てる。時刻の無い告知は日付のみ確定し notes に要確認と記す。

    注: 同一記事内で同一 (球場, チャネル) が別日程で複数回告知された場合、source_key が同一のため
    後続は取り込まない（1 発売告知＝1 スケジュールの粒度。実データ上は稀）。球場を特定できない
    発売文（地方球場など未登録球場）は警告ログに残す。
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
        # 全角の括弧・コロン・数字を半角へ寄せてから解析する（表記ゆれ耐性）
        text = unicodedata.normalize("NFKC", " ".join(block.get_text(" ", strip=True).split()))
        if not _SALE_RE.search(text):
            continue
        produced = 0
        current_venues: list[tuple[str, str]] = []
        for clause in _CLAUSE_SPLIT_RE.split(text):
            segments = list(_SALE_RE.finditer(clause))
            assigned, current_venues = _assign_clause(clause, segments, current_venues)
            for match, venues in assigned:
                mo, day, hh, mm, channel = match.groups()
                for venue_name, venue_slug in venues:
                    source_key = f"hanshin:{article_id}:{venue_slug}:{_CHANNELS[channel]}"
                    if source_key in seen_keys:
                        # source_key は差分検知の安定キーなので日付は含めない。同一 (球場,チャネル)
                        # の別日程は 1 発売告知＝1 スケジュールの粒度外なので、無警告にせずログに残す。
                        logger.warning(
                            "同一 source_key の別日程をスキップしました: %s (%s月%s日)",
                            source_key,
                            mo,
                            day,
                        )
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
                    produced += 1
        if produced == 0:  # 発売日時はあるが球場を特定できない（未登録球場の可能性）
            logger.warning("球場を特定できない発売文をスキップしました: %s", text[:80])
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

    def __init__(
        self,
        fetch: Callable[[str], str] = fetcher.get_text,
        *,
        months: list[str] | None = None,
    ) -> None:
        # fetch を差し替え可能にし、パースをネットワークから切り離してテストする。
        # months 未指定なら当月＋直近数ヶ月（本番の日次実行向け）。過去月指定で初期投入/デモに使える。
        self._fetch = fetch
        self._months = months
        # 実ネットワーク取得のときだけ小休止を挟む（注入 fetch のテストは待たない）。
        self._courtesy_delay = _COURTESY_DELAY if fetch is fetcher.get_text else 0.0

    def _pause(self) -> None:
        if self._courtesy_delay:
            time.sleep(self._courtesy_delay)

    def _fetch_or_none(self, url: str) -> str | None:
        # 1 ページの取得失敗（404・一時エラー等）で run 全体を止めず、取得できた分だけ返す。
        try:
            return self._fetch(url)
        except Exception:
            logger.warning("取得に失敗したためスキップします: %s", url, exc_info=True)
            return None

    def fetch_schedules(self) -> list[SaleSchedule]:
        months = (
            self._months if self._months is not None else _recent_months(datetime.now(JST).date())
        )
        refs: dict[str, ArticleRef] = {}
        for index, year_month in enumerate(months):
            if index:
                self._pause()
            list_html = self._fetch_or_none(self.TICKET_ARCHIVE_URL.format(year_month=year_month))
            if list_html is None:
                continue
            for ref in parse_article_list(list_html):
                refs.setdefault(ref.url, ref)  # 月をまたぐ重複を排除

        schedules: list[SaleSchedule] = []
        seen_keys: set[str] = set()
        for ref in refs.values():
            self._pause()
            article_html = self._fetch_or_none(ref.url)
            if article_html is None:
                continue
            for schedule in parse_sale_article(
                article_html, article_url=ref.url, published_date=ref.published_date
            ):
                if schedule.source_key in seen_keys:
                    continue
                seen_keys.add(schedule.source_key)
                schedules.append(schedule)
        return schedules
