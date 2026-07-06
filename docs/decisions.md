# 設計決定の記録（スクレイパー）

本リポジトリ（スクレイパー）の技術・設計上の決定を集約する。合意日は 2026-07-05。
アプリ本体の収益化・通知配信・法務まわりの決定は別リポジトリ（private）で管理する。

## 1. 取得ポリシー：事実のみ抽出、生データは再配布しない

スクレイパーが取得するのは **発売日時・試合・販売区分などの事実データのみ**。取得した生データ
（HTML/PDF そのもの）は保持・再配布せず、**リポジトリにもコミットしない**。各サイトの利用規約・
robots.txt を尊重し、低頻度・適切な間隔でアクセスする。

このポリシーは、収集データを別アプリで扱う際にも一貫させる（生の日程テーブルをそのまま画面へ
再現しない）。

## 2. 言語：Python

対象球団の発売日程データは初回HTTPレスポンスのHTML/JSONに含まれ、**ヘッドレスブラウザ
（Playwright）不要**で取得可能。必要な処理は3種で、いずれも Python で一貫対応できる
（Go/Node より明確に有利）:

- 静的HTMLパース（阪神・DeNA・ヤクルト）
- SSR/SPA の初期HTML内 JSON 抽出（巨人 `__NEXT_DATA__`、広島 Nuxt payload）
- PDF 解析（中日の確定版）

**技術構成**: requests + BeautifulSoup + pdfplumber。パッケージ管理は **uv**。

## 3. 球団別アダプター方式

日程の在り処が球団ごとにバラバラ（阪神=詳細ページ / DeNA=「第N回発売概要」ニュースに分散 /
中日=PDF / 広島=年次告知1本）。かつ **告知URLが年度ごとに変わる**（例 `carp.co.jp/news/20260219_01`）
ため、各球団に「最新の告知記事を発見するロジック」が必要。共通化を避け、球団別アダプターで実装する。

`src/npb_ticket_scraper/adapters/base.py` の `TeamAdapter` を各球団が継承する。

## 4. 差分検知と「要確認」ステータス

スケジュール変更を検知したら「要確認」ステータスを立て、未検証のまま誤通知しない。
検証OKになって初めて通知対象化する。

## 5. 実行基盤・データ保存

- 実行基盤: ローカル実行で検証 → 将来 Azure Functions（Timer Trigger, **Consumption プラン**で可。
  ヘッドレス不要が確定したため）。スクレイパーは API を持たないバッチ処理。
- 状態管理: ローカルは **SQLite** ファイル（`.db` は gitignore 済み。データはコミットしない）。
- 秘密情報（将来のクラウドDB接続文字列等）はコードに書かず、環境変数 / シークレットストアで管理する。

## 6. サイト調査結果（2026-07-05、阪神+セ5球団）

| 球団 | 日程ページ形式 | ログイン | ヘッドレス | robots.txt | 難易度 |
|---|---|---|---|---|---|
| 阪神 | 静的HTML（直書き） | 不要 | 不要 | 無し(404) | 易 |
| 巨人 | SSR（Next.js/初期HTMLに埋込） | 不要 | 不要※ | 無し(404) | 中 |
| DeNA | 静的HTMLテーブル | 不要 | 不要 | 実質全許可 | 易〜中 |
| 広島 | SPA（初期HTMLにJSON埋込） | 不要 | 不要 | 全許可 | 易〜中 |
| 中日 | 静的HTML + 確定版はPDF | 不要 | 不要 | 無し(404) | 易〜中 |
| ヤクルト | 静的HTML | 不要 | 不要 | 緩い（該当パス許可） | 易〜中 |

※巨人のみ CloudFront WAF があり、ブラウザ相当のHTTPヘッダ付与が必須（JS実行は不要）。

いずれの球団も**発売スケジュールの閲覧にログインは不要**。各サイトの利用規約原文の目視確認は残課題
（調査は要約経由のため、自動アクセス禁止条項の有無を最終確認しきれていない）。

## 7. データモデルと永続層

発売スケジュールを収集・保存し差分検知するための構造。方針は **純粋ドメイン(dataclasses) +
Repository 抽象境界 + SQLite 実装**。ドメイン層と差分検知ロジックは DB 非依存の恒久資産とし、
クラウド DB へ移行する際は Repository 実装（`SqliteRepository`）を差し替えるだけで済むようにする。

### ドメイン（`models.py`）

- `Game`（試合の事実、frozen）: 自然キー = `(game_date, home_team, away_team)`。NPB は実質
  ダブルヘッダ無しのためこの3項目で一意。
- `SaleSchedule`（アダプタが返す発売の事実）: `selling_team`（販売主体＝ビジター応援席の販売元
  差異を吸収）, `sale_type`(正規化区分) + `sale_label`(原文), `membership_rank`(会員ランク原文・
  **正規化しない**), `sale_start`/`sale_end`, `games`(**多対多**＝1発売が複数試合をバンドル), 
  `official_url`(誘導先), `source_key`(**アダプタ定義の安定キー**・可変項目を含めない)。
- 同一性判定は `source_key`、内容変化の検知は `content_fingerprint()`（sale_start/sale_end/
  sale_type/membership_rank/official_url + 対象試合の自然キー集合のハッシュ）。
- Enum: `TeamId`(12球団スラッグ) / `SeasonType` / `SaleType` / `ScheduleStatus`。

### 差分検知（`ScheduleRepository.upsert_scraped`）

`source_key` で突合し fingerprint 比較で判定する（§4 の「要確認」ステータスの具体化）:

- 新規 → 追加・`NEEDS_REVIEW`・`NEW`
- 内容変化 → 更新・`NEEDS_REVIEW` に戻す・`CHANGED`・revision 記録
- 内容不変 → `last_seen_at` のみ更新・`UNCHANGED`
- 取得集合から消失 → `ARCHIVED`・`REMOVED`

**通知対象は `CONFIRMED` のみ**（`list_notifiable()`）。誤通知防止の要。時刻は `now` を引数注入し
テストの決定性を担保する。

### SQLite スキーマ（`schema.sql`）

`game` / `sale_schedule`(UNIQUE(selling_team, source_key)) / `sale_schedule_game`(多対多) /
`sale_schedule_revision`(変更履歴 diff_json)。日時は ISO 文字列(TEXT)で保存。`.db` は gitignore 済み。

## 8. 積み残し（未調査）

- 交流戦のパ・リーグ6球団のサイト調査
- 阪神アウェイ戦の「ビジター応援席」の販売主体の特定（相手球団側販売の可能性が高い）
- 各球団の利用規約原文の目視確認
