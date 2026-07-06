## Summary
- 球団別アダプターが返すドメインモデル（`Game` / `SaleSchedule` + Enum群）を確定
- DB非依存の `ScheduleRepository` 抽象境界を導入し、標準 sqlite3 実装（差分検知・多対多バンドル・変更履歴）を追加
- `adapters/base.py` の `fetch_schedules()` 戻り値型を `list[SaleSchedule]` に確定（TODO解消）

## 設計方針
- **純粋ドメイン(dataclasses) + Repository 抽象境界 + SQLite 実装**。ドメイン層と差分検知ロジックはDB非依存の恒久資産とし、クラウドDB移行時は Repository 実装を差し替えるだけで済む構成
- 同一性判定は `source_key`（アダプタ定義の安定キー）、内容変化の検知は `content_fingerprint()`
- 差分検知セマンティクス: 新規→`NEEDS_REVIEW`/`NEW`、内容変化→`NEEDS_REVIEW`に戻す/`CHANGED`/revision記録、不変→`UNCHANGED`、消失→`ARCHIVED`/`REMOVED`
- **通知対象は `CONFIRMED` のみ**（誤通知防止の要）。時刻は `now` を引数注入しテスト決定性を担保
- アーカイブ済みスケジュールの再出現は内容ハッシュ同一でも `CHANGED`（要確認へ復帰）扱いとし、黙って通知対象に戻さない

## Test plan
- [x] `uv run pytest -q` → 6 passed（NEW / UNCHANGED / CHANGED / CONFIRMED維持 / REMOVED / バンドル多対多）
- [x] `uv run ruff check .` → All checks passed
- [x] `uv run ruff format --check .` → 全ファイル整形済み
- [x] import 確認（models / repository / sqlite_repository / adapters.base）→ OK

🤖 Generated with [Claude Code](https://claude.com/claude-code)
