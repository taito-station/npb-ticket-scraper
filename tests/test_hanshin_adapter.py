"""阪神アダプターのパース単体テストと Repository 統合テスト。

HTML は実サイトのコピーではなく、構造を模した最小の合成フィクスチャを使う
（生データを再配布しない設計原則 docs/decisions.md §1 を守りつつオフライン検証する）。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from npb_ticket_scraper.adapters.hanshin import (
    HanshinAdapter,
    _classify_sale_type,
    _infer_year,
    _recent_months,
    parse_article_list,
    parse_sale_article,
)
from npb_ticket_scraper.models import SaleType, TeamId
from npb_ticket_scraper.repository import ChangeKind
from npb_ticket_scraper.sqlite_repository import SqliteRepository

JST = ZoneInfo("Asia/Tokyo")

# --- 合成フィクスチャ（構造のみ模倣） --------------------------------------------

LIST_HTML = """
<html><body>
<ul class="news-list">
  <li>[26/01/30] <a href="/news/topics/info_10705.html">チケット転売への注意喚起</a></li>
  <li>[26/01/29] <a href="/news/topics/info_10694.html">2026年レギュラーシーズン公式戦(甲子園・京セラD)の入場券発売</a></li>
  <li>[26/01/16] <a href="/news/topics/info_10652.html">2026年甲子園・京セラD開催オープン戦入場券発売</a></li>
</ul>
</body></html>
"""

# 実構造に合わせ、両球場＋全チャネルを 1 段落に「また」で連結（球場ごとに別日程）
ARTICLE_GENERAL_HTML = """
<html><head><title>2026年レギュラーシーズン公式戦(甲子園・京セラD)の入場券発売｜球団ニュース｜阪神タイガース公式サイト</title></head>
<body><div id="container"><div id="news-entry">
<p>阪神甲子園球場で開催されるセ・リーグ公式戦2026の入場券を、2月25日(水)12:00よりインターネットで、2月27日(金)10:00より各店舗にて発売いたします。また、京セラドーム大阪で開催されるセ・リーグ公式戦2026の入場券を、2月19日(木)12:00よりインターネットで、2月21日(土)10:00より各店舗にて発売いたします。</p>
<p>阪神甲子園球場開催試合 / 京セラドーム大阪開催試合</p>
</div></div></body></html>
"""

# 実構造に合わせ、複数球場に同一日程（「ならびに」）＋時刻なし発売
ARTICLE_OPENSEASON_HTML = """
<html><head><title>2026年甲子園・京セラD開催オープン戦入場券発売｜球団ニュース｜阪神タイガース公式サイト</title></head>
<body><div id="news-entry">
<p>阪神甲子園球場ならびに京セラドーム大阪で開催するオープン戦の入場券を1月30日(金)よりインターネットにて発売、また2月2日(月)より各店舗にて発売いたします。</p>
</div></body></html>
"""


def _fake_fetch(url: str) -> str:
    if "info_10694" in url:
        return ARTICLE_GENERAL_HTML
    if "info_10652" in url:
        return ARTICLE_OPENSEASON_HTML
    if "info_10705" in url:
        return "<html><body><div id='news-entry'><p>転売は禁止です。</p></div></body></html>"
    return LIST_HTML  # 月別アーカイブ


# --- parse_article_list ---------------------------------------------------------


def test_parse_article_list_extracts_sale_articles_only() -> None:
    refs = parse_article_list(LIST_HTML)

    urls = [r.url for r in refs]
    # 「発売」を含む 2 記事のみ。転売注意（発売/販売なし）は除外
    assert urls == [
        "https://hanshintigers.jp/news/topics/info_10694.html",
        "https://hanshintigers.jp/news/topics/info_10652.html",
    ]
    assert refs[0].published_date == date(2026, 1, 29)
    assert "入場券発売" in refs[0].title


# --- parse_sale_article ---------------------------------------------------------


def test_parse_sale_article_yields_venue_channel_schedules() -> None:
    schedules = parse_sale_article(
        ARTICLE_GENERAL_HTML,
        article_url="https://hanshintigers.jp/news/topics/info_10694.html",
        published_date=date(2026, 1, 29),
    )

    keys = {s.source_key for s in schedules}
    assert keys == {
        "hanshin:10694:koshien:net",
        "hanshin:10694:koshien:store",
        "hanshin:10694:kyocera:net",
        "hanshin:10694:kyocera:store",
    }
    koshien_net = next(s for s in schedules if s.source_key == "hanshin:10694:koshien:net")
    assert koshien_net.selling_team == TeamId.HANSHIN
    assert koshien_net.sale_type == SaleType.GENERAL
    assert koshien_net.sale_start == datetime(2026, 2, 25, 12, 0, tzinfo=JST)
    assert koshien_net.games == []  # PoC は球場単位（個別試合は未解決）
    assert "阪神甲子園球場" in koshien_net.sale_label
    # 「また」以降の京セラも別球場として取りこぼさない
    kyocera_net = next(s for s in schedules if s.source_key == "hanshin:10694:kyocera:net")
    assert kyocera_net.sale_start == datetime(2026, 2, 19, 12, 0, tzinfo=JST)


def test_parse_sale_article_multi_venue_and_dateonly() -> None:
    # 「ならびに」で複数球場に同一日程＋時刻なしの告知
    schedules = parse_sale_article(
        ARTICLE_OPENSEASON_HTML,
        article_url="https://hanshintigers.jp/news/topics/info_10652.html",
        published_date=date(2026, 1, 16),
    )

    keys = {s.source_key for s in schedules}
    assert keys == {
        "hanshin:10652:koshien:net",
        "hanshin:10652:kyocera:net",
        "hanshin:10652:koshien:store",
        "hanshin:10652:kyocera:store",
    }
    koshien_net = next(s for s in schedules if s.source_key == "hanshin:10652:koshien:net")
    assert koshien_net.sale_type == SaleType.GENERAL
    assert "オープン戦" in koshien_net.sale_label
    # 時刻未記載 → 日付のみ（0:00）確定し notes に要確認を記す
    assert koshien_net.sale_start == datetime(2026, 1, 30, 0, 0, tzinfo=JST)
    assert "要確認" in koshien_net.notes


# --- 補助関数 -------------------------------------------------------------------


def test_classify_sale_type() -> None:
    assert _classify_sale_type("2026年公式戦の入場券発売") == SaleType.GENERAL
    assert _classify_sale_type("ファンクラブ会員先行販売のご案内") == SaleType.FANCLUB_PRESALE
    assert _classify_sale_type("開幕戦チケット先行抽選のご案内") == SaleType.LOTTERY
    assert _classify_sale_type("シーズンシート販売について") == SaleType.SEASON_SEAT_PRESALE
    assert _classify_sale_type("グッズに関するお知らせ") == SaleType.OTHER


def test_infer_year_handles_year_wrap() -> None:
    # 公開月以降の発売月は同年
    assert _infer_year(2, date(2026, 1, 29)) == 2026
    # 公開月より小さい発売月は翌年（年末告知 → 翌年発売）
    assert _infer_year(2, date(2025, 12, 20)) == 2026


def test_recent_months_counts_back_across_year_boundary() -> None:
    assert _recent_months(date(2026, 2, 15), back=3) == ["202602", "202601", "202512", "202511"]


# --- 実データ耐性（表記ゆれ・構造ゆれ） ------------------------------------------


def _article(body: str, *, title: str = "公式戦入場券発売") -> str:
    return f"<html><head><title>{title}｜球団ニュース</title></head><body><div id='news-entry'>{body}</div></body></html>"


def test_holiday_weekday_and_fullwidth_are_parsed() -> None:
    # 祝日併記「（月・祝）」＋全角数字/括弧/コロンでも取りこぼさない
    html = _article(
        "<p>阪神甲子園球場で開催される公式戦の入場券を、２月２３日（月・祝）１２：００よりインターネットにて発売いたします。</p>"
    )
    schedules = parse_sale_article(
        html,
        article_url="https://hanshintigers.jp/news/topics/info_1.html",
        published_date=date(2026, 1, 29),
    )

    assert len(schedules) == 1
    assert schedules[0].source_key == "hanshin:1:koshien:net"
    assert schedules[0].sale_start == datetime(2026, 2, 23, 12, 0, tzinfo=JST)


def test_multiple_venues_without_matakiri_are_not_cross_bound() -> None:
    # 「また」区切りが無く球場ごとに別日程でも、日時が球場をまたいで誤結合しない
    html = _article(
        "<p>阪神甲子園球場は2月25日(水)12:00よりインターネットにて、"
        "京セラドーム大阪は2月19日(木)12:00よりインターネットにて発売いたします。</p>"
    )
    schedules = parse_sale_article(
        html,
        article_url="https://hanshintigers.jp/news/topics/info_1.html",
        published_date=date(2026, 1, 29),
    )

    by_key = {s.source_key: s for s in schedules}
    assert by_key["hanshin:1:koshien:net"].sale_start == datetime(2026, 2, 25, 12, 0, tzinfo=JST)
    assert by_key["hanshin:1:kyocera:net"].sale_start == datetime(2026, 2, 19, 12, 0, tzinfo=JST)


def test_unknown_venue_is_skipped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    html = _article(
        "<p>ほっともっとフィールド神戸で開催される公式戦の入場券を、"
        "2月1日(日)10:00より各店舗にて発売いたします。</p>"
    )
    with caplog.at_level(logging.WARNING):
        schedules = parse_sale_article(
            html,
            article_url="https://hanshintigers.jp/news/topics/info_2.html",
            published_date=date(2026, 1, 20),
        )

    assert schedules == []  # 未登録球場は取り込まない
    assert any("球場を特定できない" in r.message for r in caplog.records)


def test_phone_channel_is_recognized() -> None:
    html = _article(
        "<p>阪神甲子園球場で開催される公式戦の入場券を、2月10日(火)10:00より電話にて発売いたします。</p>"
    )
    schedules = parse_sale_article(
        html,
        article_url="https://hanshintigers.jp/news/topics/info_3.html",
        published_date=date(2026, 1, 20),
    )

    assert schedules[0].source_key == "hanshin:3:koshien:phone"


def test_article_id_missing_raises() -> None:
    with pytest.raises(ValueError, match="記事 ID"):
        parse_sale_article(
            "<p>x</p>",
            article_url="https://example.com/no-article-id",
            published_date=date(2026, 1, 1),
        )


def test_article_list_skips_rows_without_date_and_external_hosts() -> None:
    no_date = "<ul><li><a href='/news/topics/info_9.html'>特別企画チケット発売</a></li></ul>"
    assert parse_article_list(no_date) == []  # [YY/MM/DD] が無い行は除外

    external = (
        "<ul><li>[26/01/29] "
        "<a href='https://evil.example/news/topics/info_1.html'>入場券発売</a></li></ul>"
    )
    assert parse_article_list(external) == []  # 外部ホストの絶対 URL は取得対象にしない


def test_article_list_accepts_lottery_and_presale_titles() -> None:
    # 「発売/販売」を含まない抽選・先行・受付の告知も一覧で拾う（分類器の守備範囲と整合）
    html = (
        "<ul>"
        "<li>[26/01/10] <a href='/news/topics/info_20.html'>開幕戦チケット先行抽選申込受付</a></li>"
        "<li>[26/01/11] <a href='/news/topics/info_21.html'>グッズ入荷のお知らせ</a></li>"
        "</ul>"
    )
    urls = [r.url for r in parse_article_list(html)]
    assert urls == ["https://hanshintigers.jp/news/topics/info_20.html"]


def test_datetime_without_weekday_is_parsed() -> None:
    # 曜日括弧の無い日時表記でも取りこぼさない
    html = _article(
        "<p>阪神甲子園球場で開催される公式戦の入場券を、2月25日12:00よりインターネットにて発売いたします。</p>"
    )
    schedules = parse_sale_article(
        html,
        article_url="https://hanshintigers.jp/news/topics/info_5.html",
        published_date=date(2026, 1, 29),
    )

    assert schedules[0].sale_start == datetime(2026, 2, 25, 12, 0, tzinfo=JST)


def test_fetch_schedules_skips_failed_pages() -> None:
    # 1 記事の取得失敗で run 全体を止めず、取得できた分だけ返す
    def flaky_fetch(url: str) -> str:
        if "info_10694" in url:
            raise RuntimeError("boom")
        return _fake_fetch(url)

    schedules = HanshinAdapter(fetch=flaky_fetch).fetch_schedules()

    keys = {s.source_key for s in schedules}
    assert keys  # 空にならない
    assert any("10652" in k for k in keys)  # 生きている記事は取得できる
    assert not any("10694" in k for k in keys)  # 落ちた記事は含まれない


# --- Repository 統合 ------------------------------------------------------------


def test_fetch_schedules_flows_into_repository() -> None:
    adapter = HanshinAdapter(fetch=_fake_fetch)
    schedules = adapter.fetch_schedules()

    # 一般 4 件（2球場×2チャネル）＋ オープン戦 4 件 = 8 件、月をまたいでも重複しない
    assert len(schedules) == 8
    assert all(s.selling_team == TeamId.HANSHIN for s in schedules)

    with SqliteRepository() as repo:
        results = repo.upsert_scraped(
            TeamId.HANSHIN, schedules, now=datetime(2026, 2, 1, tzinfo=UTC)
        )
        assert all(r.change_kind == ChangeKind.NEW for r in results)
        assert len(repo.list_schedules()) == 8
