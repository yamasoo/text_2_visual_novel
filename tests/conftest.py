"""讓所有 TestClient 自動帶 fekit 的權杖（P1 之後所有變更請求都需要它）。

只有呼叫端「完全沒有」指定 X-FE-Token 時才補；故意傳錯權杖的測試
（可以放在單次請求的 headers，也可以放在 TestClient 的預設 headers）不受影響。
"""
import os

TOKEN = os.environ.setdefault("T2VN_FE_TOKEN", "test-token-for-tests")

from starlette.testclient import TestClient  # noqa: E402

_original_request = TestClient.request


def _has_token(headers) -> bool:
    return any(str(key).lower() == "x-fe-token" for key in (headers or ()))


def _request(self, method, url, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if not _has_token(headers) and not _has_token(getattr(self, "headers", {})):
        headers["X-FE-Token"] = TOKEN
    return _original_request(self, method, url, headers=headers, **kwargs)


TestClient.request = _request
