"""HTTP 取得ヘルパ（球団別アダプター共通）。

礼儀として識別可能な User-Agent を付与し、低頻度アクセスを前提とする（docs/decisions.md §1・§6）。
取得するのは事実抽出のための HTML のみで、生データは保持・再配布しない。
"""

from __future__ import annotations

from urllib.parse import urlparse

import requests

# 問い合わせ先を明示した識別可能な User-Agent（連絡先にリポジトリ URL を含める）。
USER_AGENT = "npb-ticket-scraper/0.1 (+https://github.com/taito-station/npb-ticket-scraper)"

DEFAULT_TIMEOUT = 15.0


def get_text(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """URL を GET して本文テキストを返す。

    HTTP エラーは ``raise_for_status`` で送出する（呼び出し側で扱う）。別ホストへリダイレクト
    された場合は取得を拒否する（意図しない外部取得＝SSRF 面の防御）。文字コードはヘッダが
    未宣言、または requests の既定フォールバック(ISO-8859-1)のときだけ ``apparent_encoding``
    で推定し直す（正しく宣言済みのレスポンスを chardet の誤推定で壊さないため）。
    """
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    if urlparse(response.url).netloc != urlparse(url).netloc:
        raise requests.TooManyRedirects(f"別ホストへのリダイレクトを拒否しました: {response.url}")
    if response.encoding is None or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding
    return response.text
