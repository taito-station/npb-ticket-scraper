"""SqliteRepository の差分検知セマンティクスの単体テスト（インメモリ SQLite）。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from npb_ticket_scraper.models import (
    Game,
    SaleSchedule,
    SaleType,
    ScheduleStatus,
    SeasonType,
    TeamId,
)
from npb_ticket_scraper.repository import ChangeKind
from npb_ticket_scraper.sqlite_repository import SqliteRepository


def _now(day: int = 1) -> datetime:
    """テスト用の決定的な時刻（2026-07-<day> 12:00 UTC）。"""
    return datetime(2026, 7, day, 12, 0, tzinfo=UTC)


def _game(day: int = 10, away: TeamId = TeamId.GIANTS) -> Game:
    return Game(
        game_date=date(2026, 7, day),
        home_team=TeamId.HANSHIN,
        away_team=away,
        venue="阪神甲子園球場",
        season_type=SeasonType.REGULAR,
        start_time=time(18, 0),
    )


def _schedule(
    *,
    source_key: str = "hanshin-2026-general-0710",
    sale_start: datetime | None = None,
    games: list[Game] | None = None,
) -> SaleSchedule:
    return SaleSchedule(
        selling_team=TeamId.HANSHIN,
        sale_type=SaleType.GENERAL,
        sale_label="一般発売",
        sale_start=sale_start or datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        sale_end=datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
        games=games or [_game()],
        official_url="https://hanshintigers.jp/ticket/2026/general.html",
        source_url="https://hanshintigers.jp/ticket/2026/",
        source_key=source_key,
        membership_rank=None,
        notes=None,
    )


@pytest.fixture
def repo() -> SqliteRepository:
    with SqliteRepository() as repository:
        yield repository


def test_new_schedule_is_needs_review(repo: SqliteRepository) -> None:
    results = repo.upsert_scraped(TeamId.HANSHIN, [_schedule()], now=_now())

    assert [r.change_kind for r in results] == [ChangeKind.NEW]
    stored = repo.list_schedules()
    assert len(stored) == 1
    assert stored[0].status == ScheduleStatus.NEEDS_REVIEW
    assert repo.list_notifiable() == []


def test_reingest_same_content_is_unchanged(repo: SqliteRepository) -> None:
    repo.upsert_scraped(TeamId.HANSHIN, [_schedule()], now=_now(1))
    results = repo.upsert_scraped(TeamId.HANSHIN, [_schedule()], now=_now(2))

    assert [r.change_kind for r in results] == [ChangeKind.UNCHANGED]
    stored = repo.list_schedules()[0]
    assert stored.status == ScheduleStatus.NEEDS_REVIEW
    assert stored.last_seen_at == _now(2)  # 観測時刻のみ更新
    assert stored.last_changed_at == _now(1)  # 内容変化は無い
    # revision は新規時の1件のみ（UNCHANGED では増えない）
    revisions = repo._conn.execute("SELECT COUNT(*) c FROM sale_schedule_revision").fetchone()
    assert revisions["c"] == 1


def test_changed_content_resets_to_needs_review_and_records_revision(
    repo: SqliteRepository,
) -> None:
    repo.upsert_scraped(TeamId.HANSHIN, [_schedule()], now=_now(1))
    schedule_id = repo.list_schedules()[0].schedule_id
    repo.mark_confirmed(schedule_id, now=_now(2))
    assert repo.list_notifiable()  # 確認済みで通知対象になっている前提

    changed = _schedule(sale_start=datetime(2026, 6, 21, 10, 0, tzinfo=UTC))
    results = repo.upsert_scraped(TeamId.HANSHIN, [changed], now=_now(3))

    assert [r.change_kind for r in results] == [ChangeKind.CHANGED]
    stored = repo.list_schedules()[0]
    assert stored.status == ScheduleStatus.NEEDS_REVIEW  # 変更で要確認へ戻る
    assert stored.last_changed_at == _now(3)
    assert repo.list_notifiable() == []  # 通知対象から外れる
    revisions = repo._conn.execute(
        "SELECT diff_json FROM sale_schedule_revision ORDER BY id"
    ).fetchall()
    assert len(revisions) == 2  # 新規 + 変更
    assert "sale_start" in revisions[-1]["diff_json"]


def test_confirmed_survives_unchanged_reingest_and_is_notifiable(
    repo: SqliteRepository,
) -> None:
    repo.upsert_scraped(TeamId.HANSHIN, [_schedule()], now=_now(1))
    schedule_id = repo.list_schedules()[0].schedule_id
    repo.mark_confirmed(schedule_id, now=_now(2))

    results = repo.upsert_scraped(TeamId.HANSHIN, [_schedule()], now=_now(3))

    assert [r.change_kind for r in results] == [ChangeKind.UNCHANGED]
    notifiable = repo.list_notifiable()
    assert len(notifiable) == 1
    assert notifiable[0].status == ScheduleStatus.CONFIRMED


def test_missing_schedule_is_archived_and_not_notifiable(repo: SqliteRepository) -> None:
    repo.upsert_scraped(TeamId.HANSHIN, [_schedule()], now=_now(1))
    schedule_id = repo.list_schedules()[0].schedule_id
    repo.mark_confirmed(schedule_id, now=_now(2))

    # 次回スクレイプでは当該 source_key が消えた（空集合）
    results = repo.upsert_scraped(TeamId.HANSHIN, [], now=_now(3))

    assert [r.change_kind for r in results] == [ChangeKind.REMOVED]
    stored = repo.list_schedules()[0]
    assert stored.status == ScheduleStatus.ARCHIVED
    assert repo.list_notifiable() == []


def test_bundle_multiple_games_roundtrip(repo: SqliteRepository) -> None:
    games = [
        _game(day=10, away=TeamId.GIANTS),
        _game(day=11, away=TeamId.GIANTS),
        _game(day=12, away=TeamId.GIANTS),
    ]
    bundle = _schedule(source_key="hanshin-2026-giants-3game-set", games=games)

    repo.upsert_scraped(TeamId.HANSHIN, [bundle], now=_now())

    stored = repo.list_schedules()[0]
    assert len(stored.schedule.games) == 3
    restored_dates = sorted(g.game_date for g in stored.schedule.games)
    assert restored_dates == [date(2026, 7, 10), date(2026, 7, 11), date(2026, 7, 12)]
    # 同一試合を共有する重複行が生じない（多対多の get-or-create）
    game_count = repo._conn.execute("SELECT COUNT(*) c FROM game").fetchone()
    assert game_count["c"] == 3
