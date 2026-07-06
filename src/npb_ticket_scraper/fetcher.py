"""HTTP 取得ヘルパ（球団別アダプター共通）。

礼儀として識別可能な User-Agent を付与し、低頻度アクセスを前提とする（docs/decisions.md §1・§6）。
取得するのは事実抽出のための HTML のみで、生データは保持・再配布しない。
"""

from __future__ import annotations

import requests

# 問い合わせ先を明示した識別可能な User-Agent（連絡先にリポジトリ URL を含める）。
USER_AGENT = "npb-ticket-scraper/0.1 (+https://github.com/taito-station/npb-ticket-scraper)"

DEFAULT_TIMEOUT = 15.0


def get_text(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """URL を GET して本文テキストを返す。

    文字コードはヘッダで確定しないサイトがあるため ``apparent_encoding`` で推定し直す。
    HTTP エラーは ``raise_for_status`` で送出する（呼び出し側で扱う）。
    """
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    return response.text
