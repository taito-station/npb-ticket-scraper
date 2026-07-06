"""#3 end-to-end PoC: 阪神の実サイトから発売スケジュールを取得し Repository に流す。

実行:
    uv run python scripts/poc_hanshin.py            # 当月＋直近数ヶ月を走査（本番相当）
    uv run python scripts/poc_hanshin.py 202601     # 指定月を走査（過去のデモ/初期投入）

実サイトへ低頻度でアクセスし（docs/decisions.md §1）、抽出した SaleSchedule を SQLite に upsert して
差分検知結果と保存内容を標準出力へ表示する。生成される .db は gitignore 済み。
"""

from __future__ import annotations

import sys

from npb_ticket_scraper.adapters.hanshin import HanshinAdapter
from npb_ticket_scraper.models import TeamId
from npb_ticket_scraper.repository import ChangeKind
from npb_ticket_scraper.sqlite_repository import SqliteRepository

_DB_PATH = "hanshin_poc.db"


def main() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    months = sys.argv[1:] or None  # 例: 202601 202602

    target = "、".join(months) if months else "直近数ヶ月"
    print(f"阪神サイトから発売スケジュールを取得中（対象: {target}）...")
    schedules = HanshinAdapter().fetch_schedules(months=months)
    print(f"取得件数: {len(schedules)}")
    for schedule in schedules:
        start = schedule.sale_start.isoformat() if schedule.sale_start else "-"
        print(f"  [{schedule.sale_type.value}] {schedule.sale_label} 発売開始={start}")
        print(f"      source_key={schedule.source_key} / {schedule.official_url}")

    with SqliteRepository(_DB_PATH) as repo:
        results = repo.upsert_scraped(TeamId.HANSHIN, schedules, now=now)
        counts: dict[ChangeKind, int] = {}
        for result in results:
            counts[result.change_kind] = counts.get(result.change_kind, 0) + 1
        print("\nupsert 結果:")
        for kind, count in counts.items():
            print(f"  {kind.value}: {count}")
        print(f"保存済みスケジュール: {len(repo.list_schedules(team=TeamId.HANSHIN))} 件")


if __name__ == "__main__":
    main()
