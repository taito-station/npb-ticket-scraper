"""``ScheduleRepository`` の SQLite 実装（Python 標準ライブラリ sqlite3 のみ）。

ローカル検証・将来の Azure Functions（Consumption）実行での状態保持を担う。ORM は使わず、
日時は ISO 文字列で保存する。クラウド DB へ移行する際は本クラスを別実装に差し替える。

注: ``sqlite3.connect`` は既定 ``check_same_thread=True`` のため、本インスタンスは単一スレッド
から使う前提。クラウド配線時（ワーカースレッド跨ぎ）は invocation 毎に接続を作るか別実装へ切替える。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time
from importlib import resources

from .models import (
    Game,
    SaleSchedule,
    SaleType,
    ScheduleStatus,
    SeasonType,
    StoredSchedule,
    TeamId,
)
from .repository import ChangeKind, ScheduleRepository, UpsertResult

# fingerprint 対象（＝内容変化として履歴に残す）フィールド。games は別途集合比較する。
_FINGERPRINT_FIELDS = ("sale_type", "membership_rank", "sale_start", "sale_end", "official_url")


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SqliteRepository(ScheduleRepository):
    """SQLite ファイル（またはインメモリ）に状態を保持する Repository 実装。"""

    def __init__(self, database: str = ":memory:") -> None:
        self._conn = sqlite3.connect(database)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        ddl = resources.files("npb_ticket_scraper").joinpath("schema.sql").read_text("utf-8")
        self._conn.executescript(ddl)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteRepository:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 書き込み ---------------------------------------------------------------

    def upsert_scraped(
        self,
        team: TeamId,
        schedules: list[SaleSchedule],
        *,
        now: datetime,
    ) -> list[UpsertResult]:
        # #2 時点の契約を事前検証する（部分書き込みを避けるため、書き込み前にまとめて弾く）:
        #  1. すべて同一の販売主体（= team）。selling_team != team の扱いは #3 で確定。
        #  2. batch 内で source_key は一意。重複はアダプタ側のバグなので静かに飲み込まない。
        # なお schedules が空のとき当該 team の既存は全て ARCHIVED 化されるため、取得失敗時
        # （サイト障害・パース失敗）に空リストで呼ばないことは呼び出し側の責務とする。
        seen_keys: set[str] = set()
        for schedule in schedules:
            if schedule.selling_team != team:
                raise ValueError(
                    f"selling_team ({schedule.selling_team.value}) が対象 team ({team.value}) と "
                    "一致しません。複数販売主体（ビジター応援席など）は #3 で対応予定です。"
                )
            if schedule.source_key in seen_keys:
                raise ValueError(f"source_key が batch 内で重複しています: {schedule.source_key}")
            seen_keys.add(schedule.source_key)

        results: list[UpsertResult] = []
        try:
            for schedule in schedules:
                fingerprint = schedule.content_fingerprint()
                existing = self._conn.execute(
                    "SELECT * FROM sale_schedule WHERE selling_team = ? AND source_key = ?",
                    (schedule.selling_team.value, schedule.source_key),
                ).fetchone()

                if existing is None:
                    schedule_id = self._insert_schedule(schedule, fingerprint, now)
                    self._link_games(schedule_id, self._resolve_game_ids(schedule))
                    self._record_revision(schedule_id, None, fingerprint, {"_created": True}, now)
                    results.append(UpsertResult(schedule.source_key, ChangeKind.NEW, schedule_id))
                    continue

                schedule_id = existing["id"]
                unchanged = (
                    existing["content_hash"] == fingerprint
                    and existing["status"] != ScheduleStatus.ARCHIVED.value
                )
                if unchanged:
                    # 内容不変なら試合の再解決も行わず、観測時刻のみ更新する。fingerprint 非対象の
                    # 試合単独更新（会場・開始時刻のみの変化）はこの経路では取りこぼす（許容）。
                    self._conn.execute(
                        "UPDATE sale_schedule SET last_seen_at = ? WHERE id = ?",
                        (_dt(now), schedule_id),
                    )
                    results.append(
                        UpsertResult(schedule.source_key, ChangeKind.UNCHANGED, schedule_id)
                    )
                    continue

                diff = self._compute_diff(existing, schedule)
                if existing["status"] == ScheduleStatus.ARCHIVED.value:
                    diff["_resurrected"] = True
                self._update_schedule(schedule_id, schedule, fingerprint, now)
                self._link_games(schedule_id, self._resolve_game_ids(schedule))
                self._record_revision(schedule_id, existing["content_hash"], fingerprint, diff, now)
                results.append(UpsertResult(schedule.source_key, ChangeKind.CHANGED, schedule_id))

            results.extend(self._archive_missing(team, seen_keys, now))
            self._conn.commit()
        except Exception:
            # batch 途中の失敗を次回 commit に持ち越さないよう、部分書き込みを破棄する。
            self._conn.rollback()
            raise
        return results

    def _resolve_game_ids(self, schedule: SaleSchedule) -> list[int]:
        return [self._get_or_create_game(g) for g in schedule.games]

    def _get_or_create_game(self, game: Game) -> int:
        # 自然キーが既存でも、可変項目（開始時刻の確定・会場変更など）を取り込むため UPSERT する。
        self._conn.execute(
            """
            INSERT INTO game
                (game_date, home_team, away_team, start_time, venue, season_type)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (game_date, home_team, away_team) DO UPDATE SET
                start_time = excluded.start_time,
                venue = excluded.venue,
                season_type = excluded.season_type
            """,
            (
                game.game_date.isoformat(),
                game.home_team.value,
                game.away_team.value,
                game.start_time.isoformat() if game.start_time else None,
                game.venue,
                game.season_type.value,
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM game WHERE game_date = ? AND home_team = ? AND away_team = ?",
            (game.game_date.isoformat(), game.home_team.value, game.away_team.value),
        ).fetchone()
        return row["id"]

    def _insert_schedule(self, schedule: SaleSchedule, fingerprint: str, now: datetime) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO sale_schedule
                (selling_team, source_key, sale_type, sale_label, membership_rank,
                 sale_start, sale_end, official_url, source_url, notes,
                 content_hash, status, first_seen_at, last_seen_at, last_changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                schedule.selling_team.value,
                schedule.source_key,
                schedule.sale_type.value,
                schedule.sale_label,
                schedule.membership_rank,
                _dt(schedule.sale_start),
                _dt(schedule.sale_end),
                schedule.official_url,
                schedule.source_url,
                schedule.notes,
                fingerprint,
                ScheduleStatus.NEEDS_REVIEW.value,
                _dt(now),
                _dt(now),
                _dt(now),
            ),
        )
        schedule_id = cursor.lastrowid
        if schedule_id is None:  # INSERT 直後は必ず採番される（防御的チェック）
            raise RuntimeError("INSERT 後に lastrowid を取得できませんでした")
        return schedule_id

    def _update_schedule(
        self, schedule_id: int, schedule: SaleSchedule, fingerprint: str, now: datetime
    ) -> None:
        self._conn.execute(
            """
            UPDATE sale_schedule SET
                sale_type = ?, sale_label = ?, membership_rank = ?,
                sale_start = ?, sale_end = ?, official_url = ?, source_url = ?, notes = ?,
                content_hash = ?, status = ?, last_seen_at = ?, last_changed_at = ?
            WHERE id = ?
            """,
            (
                schedule.sale_type.value,
                schedule.sale_label,
                schedule.membership_rank,
                _dt(schedule.sale_start),
                _dt(schedule.sale_end),
                schedule.official_url,
                schedule.source_url,
                schedule.notes,
                fingerprint,
                ScheduleStatus.NEEDS_REVIEW.value,
                _dt(now),
                _dt(now),
                schedule_id,
            ),
        )

    def _link_games(self, schedule_id: int, game_ids: list[int]) -> None:
        # 対象試合が変わり得るため一度クリアして張り直す。どの発売からも参照されなくなった
        # game 行は「試合が存在した事実」として残す方針（GC しない）。データ量は 12 球団分と
        # 小さく、履歴・再バンドル時の参照にも使えるため意図的に保持する。
        self._conn.execute("DELETE FROM sale_schedule_game WHERE schedule_id = ?", (schedule_id,))
        self._conn.executemany(
            "INSERT INTO sale_schedule_game (schedule_id, game_id) VALUES (?, ?)",
            [(schedule_id, gid) for gid in game_ids],
        )

    def _record_revision(
        self,
        schedule_id: int,
        old_hash: str | None,
        new_hash: str,
        diff: dict,
        now: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO sale_schedule_revision
                (schedule_id, changed_at, old_content_hash, new_content_hash, diff_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (schedule_id, _dt(now), old_hash, new_hash, json.dumps(diff, ensure_ascii=False)),
        )

    def _compute_diff(self, existing: sqlite3.Row, schedule: SaleSchedule) -> dict:
        """既存行と新規スケジュールの、fingerprint 対象（スカラ項目 + 対象試合集合）の差分を返す。

        content_fingerprint と同じ観点を対象にする。membership_rank は fingerprint 同様
        None と "" を同一視して比較する（正規化ポリシーを一致させる）。
        """
        new_values = {
            "sale_type": schedule.sale_type.value,
            "membership_rank": schedule.membership_rank or "",
            "sale_start": _dt(schedule.sale_start),
            "sale_end": _dt(schedule.sale_end),
            "official_url": schedule.official_url,
        }
        old_values = {
            "sale_type": existing["sale_type"],
            "membership_rank": existing["membership_rank"] or "",
            "sale_start": existing["sale_start"],
            "sale_end": existing["sale_end"],
            "official_url": existing["official_url"],
        }
        diff: dict = {}
        for field_name in _FINGERPRINT_FIELDS:
            if old_values[field_name] != new_values[field_name]:
                diff[field_name] = [old_values[field_name], new_values[field_name]]

        # 対象試合集合（バンドル）も fingerprint に含まれるため、変化を履歴に残す。
        old_games = sorted(self._load_game_keys(existing["id"]))
        new_games = sorted(g.natural_key for g in schedule.games)
        if old_games != new_games:
            diff["games"] = [old_games, new_games]
        return diff

    def _load_game_keys(self, schedule_id: int) -> set[str]:
        rows = self._conn.execute(
            """
            SELECT g.game_date, g.home_team, g.away_team FROM game g
            JOIN sale_schedule_game sg ON sg.game_id = g.id
            WHERE sg.schedule_id = ?
            """,
            (schedule_id,),
        ).fetchall()
        return {f"{r['game_date']}|{r['home_team']}|{r['away_team']}" for r in rows}

    def _archive_missing(
        self, team: TeamId, seen_keys: set[str], now: datetime
    ) -> list[UpsertResult]:
        rows = self._conn.execute(
            """
            SELECT id, source_key FROM sale_schedule
            WHERE selling_team = ? AND status != ?
            """,
            (team.value, ScheduleStatus.ARCHIVED.value),
        ).fetchall()
        results: list[UpsertResult] = []
        for row in rows:
            if row["source_key"] in seen_keys:
                continue
            self._conn.execute(
                "UPDATE sale_schedule SET status = ?, last_changed_at = ? WHERE id = ?",
                (ScheduleStatus.ARCHIVED.value, _dt(now), row["id"]),
            )
            results.append(UpsertResult(row["source_key"], ChangeKind.REMOVED, row["id"]))
        return results

    def mark_confirmed(self, schedule_id: int, *, now: datetime) -> None:
        # ARCHIVED（取得元から消失）を確認済みに昇格させない。消えたものを通知対象へ戻さない。
        self._conn.execute(
            "UPDATE sale_schedule SET status = ?, last_changed_at = ? WHERE id = ? AND status != ?",
            (
                ScheduleStatus.CONFIRMED.value,
                _dt(now),
                schedule_id,
                ScheduleStatus.ARCHIVED.value,
            ),
        )
        self._conn.commit()

    # -- 読み取り ---------------------------------------------------------------

    def list_schedules(
        self,
        *,
        team: TeamId | None = None,
        status: ScheduleStatus | None = None,
    ) -> list[StoredSchedule]:
        query = "SELECT * FROM sale_schedule"
        conditions: list[str] = []
        params: list[str] = []
        if team is not None:
            conditions.append("selling_team = ?")
            params.append(team.value)
        if status is not None:
            conditions.append("status = ?")
            params.append(status.value)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id"
        rows = self._conn.execute(query, params).fetchall()
        # 各行で試合を個別クエリする N+1 だが、対象は 12 球団・高々数十件のため許容する
        # （JOIN 一括ロードへの最適化は規模が問題化してから）。
        return [self._load_schedule(row) for row in rows]

    def list_notifiable(self) -> list[StoredSchedule]:
        return self.list_schedules(status=ScheduleStatus.CONFIRMED)

    def _load_schedule(self, row: sqlite3.Row) -> StoredSchedule:
        games = self._load_games(row["id"])
        schedule = SaleSchedule(
            selling_team=TeamId(row["selling_team"]),
            sale_type=SaleType(row["sale_type"]),
            sale_label=row["sale_label"],
            sale_start=_parse_dt(row["sale_start"]),
            sale_end=_parse_dt(row["sale_end"]),
            games=games,
            official_url=row["official_url"],
            source_url=row["source_url"],
            source_key=row["source_key"],
            membership_rank=row["membership_rank"],
            notes=row["notes"],
        )
        return StoredSchedule(
            schedule_id=row["id"],
            schedule=schedule,
            status=ScheduleStatus(row["status"]),
            content_hash=row["content_hash"],
            first_seen_at=_parse_dt(row["first_seen_at"]),
            last_seen_at=_parse_dt(row["last_seen_at"]),
            last_changed_at=_parse_dt(row["last_changed_at"]),
        )

    def _load_games(self, schedule_id: int) -> list[Game]:
        rows = self._conn.execute(
            """
            SELECT g.* FROM game g
            JOIN sale_schedule_game sg ON sg.game_id = g.id
            WHERE sg.schedule_id = ?
            ORDER BY g.game_date, g.home_team, g.away_team
            """,
            (schedule_id,),
        ).fetchall()
        return [
            Game(
                game_date=date.fromisoformat(row["game_date"]),
                home_team=TeamId(row["home_team"]),
                away_team=TeamId(row["away_team"]),
                venue=row["venue"],
                season_type=SeasonType(row["season_type"]),
                start_time=time.fromisoformat(row["start_time"]) if row["start_time"] else None,
            )
            for row in rows
        ]
