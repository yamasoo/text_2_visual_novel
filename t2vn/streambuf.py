"""任務輸出緩衝：讓網頁邊跑邊看模型輸出。

背景任務（AI 標註、抽取章綱）呼叫語言模型時，模型會用串流一小段一小段送回文字。
任務執行緒一直呼叫 :meth:`StreamBuffer.add`，網頁每隔一秒用「游標」呼叫
:meth:`StreamBuffer.read` 取走新的部分，就能像聊天視窗那樣邊跑邊看。

事件依序編號（``seq`` 從 1 開始，永不重複、永不回頭）；文字與「思考過程」分開記。
緩衝內的文字總字數超過上限時，從最舊的事件開始丟，並讓落後的讀者知道「有漏」
（``truncated``）。所有公開方法都用 ``threading.Lock`` 保護。
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any

DEFAULT_MAX_CHARS = 400_000
TEXT_TYPES = frozenset({"text", "thinking"})


class StreamBuffer:
    """執行緒安全的任務輸出緩衝。"""

    def __init__(self, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        """``max_chars``：緩衝內所有 text／thinking 事件的字數總和上限。"""
        self._lock = threading.Lock()
        self._max_chars = max_chars
        self._events: deque[dict[str, Any]] = deque()
        self._chars = 0
        self._seq = 0
        self._seg = 0
        self._open_seg: int | None = None

    # ------------------------------------------------------------ 內部（呼叫時已持鎖）

    def _append(self, event: dict[str, Any]) -> None:
        """把事件加上序號放進佇列，必要時丟掉最舊的事件。"""
        self._seq += 1
        event["seq"] = self._seq
        self._events.append(event)
        if event["type"] in TEXT_TYPES:
            self._chars += len(event["text"])
        self._trim()

    def _trim(self) -> None:
        """從最舊的事件開始丟，直到字數總和不超過上限。"""
        while self._chars > self._max_chars and self._events:
            dropped = self._events.popleft()
            if dropped["type"] in TEXT_TYPES:
                self._chars -= len(dropped["text"])

    def _open_segment(self, label: str) -> int:
        """開新段落（呼叫前必須已持鎖），回傳段落編號。"""
        self._seg += 1
        self._open_seg = self._seg
        self._append({"type": "begin", "seg": self._seg, "label": label})
        return self._seg

    def _close_segment(self, note: str) -> None:
        """結束目前段落（呼叫前必須已持鎖）；沒有開著的段落就什麼都不做。"""
        if self._open_seg is None:
            return
        self._append({"type": "end", "seg": self._open_seg, "note": note})
        self._open_seg = None

    # ------------------------------------------------------------ 公開介面

    def clear(self) -> None:
        """清空所有事件，序號與段落編號都歸零。"""
        with self._lock:
            self._events.clear()
            self._chars = 0
            self._seq = 0
            self._seg = 0
            self._open_seg = None

    def begin(self, label: str) -> int:
        """開始新的一段並回傳段落編號（從 1 起，遞增）。

        若目前有開著的段落，先自動結束它（產生一個 ``end`` 事件，``note`` 為空字串）。
        """
        with self._lock:
            self._close_segment("")
            return self._open_segment(label)

    def add(self, text: str, thinking: bool = False) -> None:
        """在目前段落追加文字（``thinking=True`` 則是「思考過程」）。

        空字串直接忽略；若目前沒有開著的段落，自動開一個 ``label`` 為空字串的新段落。
        """
        if not text:
            return
        with self._lock:
            if self._open_seg is None:
                self._open_segment("")
            kind = "thinking" if thinking else "text"
            self._append({"type": kind, "seg": self._open_seg, "text": text})

    def end(self, note: str = "") -> None:
        """結束目前段落；沒有開著的段落時什麼都不做。"""
        with self._lock:
            self._close_segment(note)

    def read(self, since: int = 0) -> dict[str, Any]:
        """回傳 ``{"events": [...], "last": int, "truncated": bool}``。

        ``events``：序號大於 ``since`` 的事件（依序號排序）。``last``：目前最大的序號
        （沒有任何事件時為 0）。``truncated``：呼叫者想要的事件有一部分已被丟掉。
        """
        with self._lock:
            events = [dict(event) for event in self._events if event["seq"] > since]
            oldest = self._events[0]["seq"] if self._events else 0
            truncated = oldest > 0 and (oldest - 1) > since
            return {"events": events, "last": self._seq, "truncated": truncated}
