"""t2vn/streambuf.py 的驗收測試：任務執行中，模型輸出的文字要能被網頁用「游標」增量取走。"""
import threading

from t2vn.streambuf import StreamBuffer


def test_empty_buffer():
    buf = StreamBuffer()
    assert buf.read() == {"events": [], "last": 0, "truncated": False}
    assert buf.read(since=5) == {"events": [], "last": 0, "truncated": False}


def test_begin_add_end_sequence():
    buf = StreamBuffer()
    assert buf.begin("第 3 章 · 第 1 輪") == 1
    buf.add("你好")
    buf.add("世界")
    buf.add("先想一下", thinking=True)
    buf.end("ok")
    got = buf.read()
    assert got["last"] == 5 and got["truncated"] is False
    assert got["events"] == [
        {"seq": 1, "type": "begin", "seg": 1, "label": "第 3 章 · 第 1 輪"},
        {"seq": 2, "type": "text", "seg": 1, "text": "你好"},
        {"seq": 3, "type": "text", "seg": 1, "text": "世界"},
        {"seq": 4, "type": "thinking", "seg": 1, "text": "先想一下"},
        {"seq": 5, "type": "end", "seg": 1, "note": "ok"},
    ]


def test_read_since_returns_only_new_events():
    buf = StreamBuffer()
    buf.begin("a")
    buf.add("x")
    cursor = buf.read()["last"]
    assert buf.read(since=cursor) == {"events": [], "last": cursor, "truncated": False}
    buf.add("y")
    got = buf.read(since=cursor)
    assert [e["text"] for e in got["events"]] == ["y"] and got["last"] == cursor + 1


def test_segments_get_increasing_ids_and_add_goes_to_current():
    buf = StreamBuffer()
    assert buf.begin("one") == 1
    buf.add("a")
    buf.end()
    assert buf.begin("two") == 2
    buf.add("b")
    segs = [(e["seg"], e["text"]) for e in buf.read()["events"] if e["type"] == "text"]
    assert segs == [(1, "a"), (2, "b")]


def test_add_without_begin_opens_unnamed_segment():
    buf = StreamBuffer()
    buf.add("hello")
    events = buf.read()["events"]
    assert events[0] == {"seq": 1, "type": "begin", "seg": 1, "label": ""}
    assert events[1] == {"seq": 2, "type": "text", "seg": 1, "text": "hello"}


def test_begin_closes_an_open_segment_first():
    buf = StreamBuffer()
    buf.begin("one")
    buf.begin("two")
    types = [(e["type"], e["seg"]) for e in buf.read()["events"]]
    assert types == [("begin", 1), ("end", 1), ("begin", 2)]


def test_end_without_open_segment_is_noop():
    buf = StreamBuffer()
    buf.end("x")
    assert buf.read()["events"] == []
    buf.begin("a")
    buf.end()
    buf.end()
    assert [e["type"] for e in buf.read()["events"]] == ["begin", "end"]


def test_empty_text_is_ignored():
    buf = StreamBuffer()
    buf.begin("a")
    buf.add("")
    assert [e["type"] for e in buf.read()["events"]] == ["begin"]


def test_truncation_drops_oldest_and_reports_it():
    buf = StreamBuffer(max_chars=10)
    buf.begin("a")
    for i in range(10):
        buf.add("abcde")             # 共 50 字，上限 10 字
    got = buf.read(since=0)
    assert got["truncated"] is True
    assert sum(len(e.get("text", "")) for e in got["events"]) <= 10
    assert got["events"][-1]["seq"] == got["last"] == 11
    seqs = [e["seq"] for e in got["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    fresh = buf.read(since=got["last"])
    assert fresh["truncated"] is False and fresh["events"] == []


def test_up_to_date_reader_is_not_truncated():
    buf = StreamBuffer(max_chars=10)
    buf.begin("a")
    cursor = 0
    for _ in range(10):
        buf.add("abcde")
        got = buf.read(since=cursor)
        assert got["truncated"] is False   # 每次都跟上，就不會漏
        cursor = got["last"]


def test_clear_resets_everything():
    buf = StreamBuffer()
    buf.begin("a")
    buf.add("x")
    buf.clear()
    assert buf.read() == {"events": [], "last": 0, "truncated": False}
    assert buf.begin("b") == 1


def test_thread_safety():
    buf = StreamBuffer()
    buf.begin("a")

    def worker():
        for _ in range(200):
            buf.add("z")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = buf.read()["events"]
    assert len(events) == 801
    assert [e["seq"] for e in events] == list(range(1, 802))
