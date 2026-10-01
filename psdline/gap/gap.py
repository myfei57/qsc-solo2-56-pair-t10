"""间隙探测登记。"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import InterlockError
from ..store import JsonStore

CLEAR = "clear"
BLOCKED = "blocked"


@dataclass(frozen=True)
class ProbeResult:
    """一次间隙探测结果。"""

    train_id: str
    clear: bool
    confirmed: bool
    at: float

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "clear": self.clear,
            "confirmed": self.confirmed,
            "at": self.at,
        }


class GapRegistry:
    """间隙探测登记表。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._checks = 0
        self._probes = 0

    @property
    def checks(self) -> int:
        """返回累计确认次数。"""

        return self._checks

    @property
    def probes(self) -> int:
        """返回累计探测次数。"""

        return self._probes

    @staticmethod
    def _doc_id(train_id: str) -> str:
        return f"gap/{train_id}"

    def check(self, train_id: str, at: float) -> ProbeResult:
        """确认间隙无异常；未对位的列车直接拒绝。"""

        if not self._store.exists(f"train/{train_id}/align"):
            raise InterlockError(f"列车 {train_id} 尚未对位，无法确认间隙", component="gap")
        self._store.write(
            self._doc_id(train_id),
            {"train_id": train_id, "state": CLEAR, "at": at},
        )
        self._checks += 1
        return ProbeResult(train_id=train_id, clear=True, confirmed=True, at=at)

    def clear(self, train_id: str) -> bool:
        """查询间隙是否已确认无异常。"""

        raw = self._store.read(self._doc_id(train_id))
        return bool(isinstance(raw, dict) and raw.get("state") == CLEAR)

    def probe(self, train_id: str, clear: bool, at: float) -> ProbeResult:
        """登记一次原始探测结果（可含可疑）。"""

        state = CLEAR if clear else BLOCKED
        self._store.write(
            self._doc_id(train_id),
            {"train_id": train_id, "state": state, "at": at},
        )
        self._probes += 1
        return ProbeResult(train_id=train_id, clear=clear, confirmed=clear, at=at)

    def state(self, train_id: str) -> str:
        """返回列车当前的间隙状态（``clear`` / ``blocked`` / 空串）。"""

        raw = self._store.read(self._doc_id(train_id))
        if isinstance(raw, dict):
            return str(raw.get("state", ""))
        return ""

    def blocked_ids(self) -> list[str]:
        """返回当前处于可疑状态的列车。"""

        found: list[str] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("gap/"):
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict) and raw.get("state") == BLOCKED:
                found.append(str(raw.get("train_id", "")))
        return sorted(found)
