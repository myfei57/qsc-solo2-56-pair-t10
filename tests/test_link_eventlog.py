"""链路事件哈希链：只追加、可校验、篡改即断。"""

from __future__ import annotations

import json

import pytest

from psdline.config import default_config
from psdline.link import LinkEventLog, TamperError
from psdline.link.eventlog import SAMPLE_RECEIVED, SAMPLE_REJECTED
from psdline.store import JsonlJournal


def _log(tmp_path, topic="link"):
    journal = JsonlJournal(str(tmp_path), topic, segment_limit=4)
    return LinkEventLog(journal)


def test_events_chain_in_order(tmp_path):
    log = _log(tmp_path)
    e1 = log.append(1.0, "T01", SAMPLE_RECEIVED, {"score": 1.0})
    e2 = log.append(2.0, "T01", SAMPLE_REJECTED, {"reason": "replayed_seq"})
    e3 = log.append(3.0, "T02", SAMPLE_RECEIVED, {"score": 0.5})
    assert e1.seq == 1 and e2.seq == 2 and e3.seq == 3
    assert e1.prev_hash == "0" * 64
    assert e2.prev_hash == e1.hash
    assert e3.prev_hash == e2.hash
    assert log.verify()["ok"] is True
    assert [e.seq for e in log.for_train("T01")] == [1, 2]


def test_append_many_is_atomic(tmp_path):
    log = _log(tmp_path)
    log.append_many([
        (1.0, "T01", SAMPLE_RECEIVED, {"n": 1}),
        (2.0, "T01", SAMPLE_RECEIVED, {"n": 2}),
    ])
    assert len(log) == 2
    assert log.verify()["ok"] is True


def test_rebuild_from_journal_reverifies(tmp_path):
    journal = JsonlJournal(str(tmp_path), "link", segment_limit=2)
    log = LinkEventLog(journal)
    for i in range(1, 6):
        log.append(float(i), "T01", SAMPLE_RECEIVED, {"i": i})
    # 新实例从落盘重建，链必须仍然自洽。
    rebuilt = LinkEventLog(JsonlJournal(str(tmp_path), "link", segment_limit=2))
    assert len(rebuilt) == 5
    assert rebuilt.verify()["tail"] == log.tail_hash()


def test_tampering_a_record_breaks_chain(tmp_path):
    journal = JsonlJournal(str(tmp_path), "link", segment_limit=10)
    log = LinkEventLog(journal)
    log.append(1.0, "T01", SAMPLE_RECEIVED, {"score": 1.0})
    log.append(2.0, "T01", SAMPLE_RECEIVED, {"score": 0.9})
    # 直接改写落盘记录（模拟历史被改），重新加载必须报错。
    path = journal.segments()[0]
    lines = path and open(path, encoding="utf-8").read().splitlines()
    record = json.loads(lines[0])
    record["payload"]["score"] = 0.01
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            if line == lines[0]:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            else:
                handle.write(line + "\n")
    with pytest.raises(TamperError):
        LinkEventLog(JsonlJournal(str(tmp_path), "link", segment_limit=10))
