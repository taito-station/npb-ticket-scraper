"""ドメインモデル（発売スケジュールの「事実」表現）。

球団別アダプターが返す純粋なデータ構造を定義する。永続層（Repository 実装）や特定の DB に
依存しない恒久資産として扱う。設計の背景は docs/decisions.md を参照。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum


class TeamId(str, Enum):
    """NPB12球団の安定スラッグ。

    告知ページの年次変動や表記ゆれに影響されない内部識別子。MVPでは阪神＋セ・リーグ5球団を
    実使用するが、交流戦・将来拡張のためパ・リーグ6球団も定義しておく。
    """

    # セ・リーグ
    HANSHIN = "hanshin"
    GIANTS = "giants"
    DENA = "dena"
    HIROSHIMA = "hiroshima"
    CHUNICHI = "chunichi"
    YAKULT = "yakult"
    # パ・リーグ
    SOFTBANK = "softbank"
    LOTTE = "lotte"
    RAKUTEN = "rakuten"
    SEIBU = "seibu"
    ORIX = "orix"
    NIPPON_HAM = "nippon_ham"


class SeasonType(str, Enum):
    """試合の種別。"""

    PRESEASON = "preseason"  # オープン戦
    REGULAR = "regular"  # 公式戦


class SaleType(str, Enum):
    """発売区分。球団横断で通知の粒度をそろえるための正規化カテゴリ。

    原文ラベルは ``SaleSchedule.sale_label`` に別途保持し、ここでは通知ロジックが扱いやすい
    粗い分類に落とす。
    """

    LOTTERY = "lottery"  # 先行抽選
    FANCLUB_PRESALE = "fanclub_presale"  # ファンクラブ・会員先行
    SEASON_SEAT_PRESALE = "season_seat_presale"  # シーズンシート保有者先行
    GENERAL = "general"  # 一般発売
    STADIUM = "stadium"  # 球場窓口
    RESALE = "resale"  # リセール
    OTHER = "other"


class ScheduleStatus(str, Enum):
    """保存済みスケジュールの検証ステータス。

    誤通知を防ぐため、通知対象は ``CONFIRMED`` のみとする（docs/decisions.md §4）。
    """

    NEEDS_REVIEW = "needs_review"  # 要確認・未検証（新規／変更検知直後）
    CONFIRMED = "confirmed"  # 検証済み・通知可
    ARCHIVED = "archived"  # 取得元から消失


@dataclass(frozen=True, slots=True)
class Game:
    """試合の事実。

    自然キーは ``(game_date, home_team, away_team)``。NPB は実質ダブルヘッダが無いため、
    この3項目で一意に識別できる。
    """

    game_date: date
    home_team: TeamId
    away_team: TeamId
    venue: str
    season_type: SeasonType
    start_time: time | None = None

    @property
    def natural_key(self) -> str:
        """自然キーの文字列表現（fingerprint・突合に用いる）。"""
        return f"{self.game_date.isoformat()}|{self.home_team.value}|{self.away_team.value}"


@dataclass(slots=True)
class SaleSchedule:
    """アダプターが返す「発売の事実」。

    1回の発売が複数試合をバンドルする場合があるため ``games`` は複数を持つ（多対多）。
    同一性判定は ``source_key`` で行い、内容変化の検知は ``content_fingerprint()`` で行う。
    """

    selling_team: TeamId  # 販売主体（ビジター応援席の販売元差異を吸収）
    sale_type: SaleType
    sale_label: str  # 原文の発売区分ラベル（表示・監査用）
    sale_start: datetime | None  # 発売開始日時（通知基準）
    sale_end: datetime | None  # 発売終了日時
    games: list[Game]  # 対象試合（バンドル対応）
    official_url: str  # ユーザーを誘導する公式ページURL
    source_url: str  # 取得元URL
    source_key: str  # アダプタ定義の安定キー（team名前空間内で一意・可変項目を含めない）
    membership_rank: str | None = None  # 会員ランク原文（正規化しない）
    notes: str | None = None

    def content_fingerprint(self) -> str:
        """差分検知用のコンテンツハッシュ。

        ``source_key`` で同一と判定したスケジュール同士の「中身が変わったか」を比較する。
        対象は通知・誘導に影響する項目（発売日時・区分・会員ランク・誘導先URL・対象試合集合）。
        取得元URL(``source_url``)やラベル文言は同一性の本質でないため含めない。試合集合は
        自然キーでソートして順序非依存にする。
        """
        game_keys = sorted(g.natural_key for g in self.games)
        parts = [
            self.sale_type.value,
            self.membership_rank or "",
            self.sale_start.isoformat() if self.sale_start else "",
            self.sale_end.isoformat() if self.sale_end else "",
            self.official_url,
            *game_keys,
        ]
        payload = "\x1f".join(parts)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class StoredSchedule:
    """永続層から復元したスケジュール（保存メタ情報を含む）。

    ``SaleSchedule``（取得した事実）に、DB が付与する id・ステータス・観測時刻を加えたもの。
    Repository の読み取り系メソッドが返す。
    """

    schedule_id: int
    schedule: SaleSchedule
    status: ScheduleStatus
    content_hash: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_changed_at: datetime
