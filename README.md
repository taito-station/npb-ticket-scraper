# npb-ticket-scraper

NPB（プロ野球）のチケット販売スケジュールを収集する Python スクレイパー。
球団ごとにサイト構造・会員ランク体系が大きく異なるため、共通化を避けた **球団別アダプター方式** で実装する。

まずは阪神タイガース戦（甲子園ホーム + ビジター戦）の発売スケジュールが対象。
収集した情報は、別アプリからのチケット発売通知に利用する。

## 特徴・方針

- **ヘッドレスブラウザ不要**: 対象球団の発売日程は初回HTTPレスポンスのHTML/JSON/PDFに含まれるため、
  `requests` + `BeautifulSoup` + `pdfplumber` で取得できる（調査で実証済み。詳細は [docs/decisions.md](docs/decisions.md)）。
- **球団別アダプター**: 各球団は `TeamAdapter`（`src/npb_ticket_scraper/adapters/base.py`）を継承して実装する。
- **取得ポリシー**: 発売日時・試合・販売区分などの**事実のみ**を抽出する。取得した生データ（HTML/PDF）は
  リポジトリにコミットしない。各サイトの利用規約・robots.txt を尊重し、低頻度・適切な間隔でアクセスする。

## セットアップ

```sh
uv sync   # .venv 生成 + 依存解決
```

## 構成

```
npb-ticket-scraper/
├── pyproject.toml
├── docs/decisions.md          # 技術・設計の決定記録
└── src/npb_ticket_scraper/
    ├── __init__.py
    └── adapters/
        ├── __init__.py
        └── base.py            # TeamAdapter 抽象基底。各球団アダプタはこれを継承
```

## 開発フェーズ

現在: リポジトリ骨組みの確定（完了）。
次: データモデル確定 → 阪神アダプタの PoC（公開ページ→事実抽出→SQLite保存→差分検知）。
