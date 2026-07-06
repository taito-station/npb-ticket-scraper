"""永続層の抽象境界。

ドメイン（models.py）と具体的な DB 実装の間に置く境界。ローカルは SQLite 実装
（sqlite_repository.py）だが、クラウド移行時は本 ABC を満たす別実装へ差し替えるだけで済む
ようにする（端だけ差し替える思想。docs/decisions.md §5）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .models import SaleSchedule, ScheduleStatus, StoredSchedule, TeamId


class ChangeKind(str, Enum):
    """1件のスクレイプ結果を反映した際の変化種別。"""

    NEW = "new"  # 新規 source_key
    CHANGED = "changed"  # 既存だが内容が変化した
    UNCHANGED = "unchanged"  # 既存で内容不変（観測時刻のみ更新）
    REMOVED = "removed"  # 今回の取得集合から消えた（ARCHIVED 化）


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """``upsert_scraped`` が返す1件ぶんの反映結果。"""

    source_key: str
    change_kind: ChangeKind
    schedule_id: int


class ScheduleRepository(ABC):
    """発売スケジュールの永続化・差分検知を担う抽象境界。"""

    @abstractmethod
    def upsert_scraped(
        self,
        team: TeamId,
        schedules: list[SaleSchedule],
        *,
        now: datetime,
    ) -> list[UpsertResult]:
        """1回のスクレイプ結果を反映し、各件の変化種別を返す。

        ``source_key`` で既存と突合し、``content_fingerprint()`` の比較で内容変化を判定する。
        差分検知セマンティクス:

        - 新規 source_key → 追加し ``NEEDS_REVIEW`` / ``NEW``
        - 既存で内容変化 → 更新し ``NEEDS_REVIEW`` に戻す / ``CHANGED`` / revision 記録
        - 既存で内容不変 → ``last_seen_at`` のみ更新 / status 不変 / ``UNCHANGED``
        - 今回 ``team`` の取得集合に無い既存 → ``ARCHIVED`` / ``REMOVED``

        fingerprint 非対象の項目（``sale_label`` / ``notes`` / ``source_url``）は同一性・
        通知内容に影響しないため、``UNCHANGED`` 時には再保存しない（``CHANGED`` 時にまとめて
        更新される）。

        Args:
            team: 今回スクレイプした販売主体。``ARCHIVED`` 判定はこの team の範囲で行う。
            schedules: 取得したスケジュール。#2 時点の契約として、すべて ``selling_team == team``
                であることを要求し、満たさない場合は ``ValueError`` を送出する。
            now: 観測時刻。テスト決定性のため注入する（呼び出し側は通常 ``datetime.now(UTC)``）。

        Returns:
            入力 ``schedules`` に対応する反映結果（``REMOVED`` は入力に無いため末尾に追加され得る）。
        """
        raise NotImplementedError

    @abstractmethod
    def list_schedules(
        self,
        *,
        team: TeamId | None = None,
        status: ScheduleStatus | None = None,
    ) -> list[StoredSchedule]:
        """保存済みスケジュールを取得する（team / status で絞り込み可）。"""
        raise NotImplementedError

    @abstractmethod
    def mark_confirmed(self, schedule_id: int, *, now: datetime) -> None:
        """要確認スケジュールを検証済み（``CONFIRMED``）にする。"""
        raise NotImplementedError

    @abstractmethod
    def list_notifiable(self) -> list[StoredSchedule]:
        """通知対象（``status == CONFIRMED``）のスケジュールのみを返す。

        誤通知防止の要。``NEEDS_REVIEW`` / ``ARCHIVED`` は決して含めない。
        """
        raise NotImplementedError
