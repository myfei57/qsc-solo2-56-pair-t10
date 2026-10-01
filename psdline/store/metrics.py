"""存储用量统计。

控制台要回答"落盘是不是还在长"，巡检要确认分段有没有正常滚动，因此这里按目录
统计文件数、字节数与分段数，不读文件内容。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DirectoryUsage:
    """一个目录的用量。"""

    path: str
    files: int
    bytes: int

    def as_dict(self) -> dict:
        return {"path": self.path, "files": self.files, "bytes": self.bytes}


class StoreMetrics:
    """对存储目录做只读统计。"""

    def __init__(self, root: str) -> None:
        self._root = os.path.abspath(root)

    @property
    def root(self) -> str:
        return self._root

    def usage(self, path: str | None = None) -> DirectoryUsage:
        """统计某个子目录（默认根目录）的文件数与字节数。"""

        target = self._root if path is None else os.path.join(self._root, path)
        files = 0
        total = 0
        for base, _dirs, names in os.walk(target):
            for name in names:
                full = os.path.join(base, name)
                if os.path.isfile(full):
                    files += 1
                    total += os.path.getsize(full)
        relative = os.path.relpath(target, self._root).replace(os.sep, "/")
        return DirectoryUsage(path=relative, files=files, bytes=total)

    def segments(self, topic: str) -> int:
        """统计某主题已经滚出的分段数。"""

        return len(
            [
                name
                for name in os.listdir(self._root)
                if name.startswith(f"{topic}-") and name.endswith(".jsonl")
            ]
        )

    def summary(self) -> dict:
        """汇总存储用量。"""

        overall = self.usage()
        return {
            "root": self._root,
            "files": overall.files,
            "bytes": overall.bytes,
            "audit_segments": self.segments("audit"),
        }
