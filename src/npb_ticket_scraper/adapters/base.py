"""球団別アダプターの抽象基底。

球団ごとにサイト構造・会員ランク体系・告知の在り方が大きく異なるため、共通化を避け、
各球団はこの ``TeamAdapter`` を継承して自前の取得ロジックを実装する。

告知URLが年度ごとに変わる球団が多いため、アダプターは「最新の告知ページを発見する」責務も持つ
（詳細は docs/decisions.md の §3 を参照）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# TODO(#2): データモデル確定後、fetch_schedules の戻り値を SaleSchedule のリストに置き換える。
#           現時点ではデータモデル未確定のため、戻り値型は list のプレースホルダとする。


class TeamAdapter(ABC):
    """1球団ぶんの販売スケジュール取得を担うアダプターの基底クラス。"""

    #: 球団の識別子（例: "hanshin"）。サブクラスで定義する。
    team_id: str

    @abstractmethod
    def fetch_schedules(self) -> list:
        """公開ページから発売スケジュールの「事実」を抽出して返す。

        取得するのは発売日時・試合・販売区分などの事実データのみ。生の日程テーブルを
        そのまま再配布しない設計原則（docs/decisions.md §1）に従うこと。

        Returns:
            発売スケジュールを表すオブジェクトのリスト。具体型は #2 で確定する。
        """
        raise NotImplementedError
