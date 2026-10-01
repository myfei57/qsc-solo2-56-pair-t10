"""原子 JSON 文档存储。

每个文档一个 ``<doc_id>.json`` 文件，``doc_id`` 可以带 ``/`` 变成子目录（例如
``station/ST1/layout``）。写入时先写同目录下的临时文件再 ``os.replace`` 原子
替换，保证中途掉电不会留下半截文档；读取时容忍文件不存在（返回 ``None``），由
调用方决定是否使用默认值。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from ..errors import StorageError


class JsonStore:
    """目录形式的 JSON 文档存储。"""

    def __init__(self, root: str) -> None:
        self._root = os.path.abspath(root)
        self._lock = threading.RLock()
        os.makedirs(self._root, exist_ok=True)

    @property
    def root(self) -> str:
        return self._root

    def path_of(self, doc_id: str) -> str:
        """返回某文档对应的文件路径。"""

        relative = doc_id.replace("\\", "/").strip("/")
        if not relative:
            raise StorageError("文档标识不能为空", component="store")
        return os.path.join(self._root, *relative.split("/")) + ".json"

    def exists(self, doc_id: str) -> bool:
        """文档文件是否存在。"""

        return os.path.isfile(self.path_of(doc_id))

    def write(self, doc_id: str, payload: Any) -> str:
        """原子写入一个文档，返回最终文件路径。"""

        target = self.path_of(doc_id)
        directory = os.path.dirname(target)
        with self._lock:
            try:
                os.makedirs(directory, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=directory,
                    prefix=".write.",
                    suffix=".tmp",
                    delete=False,
                )
                with handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, target)
            except OSError as error:
                raise StorageError(f"写入文档 {doc_id} 失败: {error}", component="store") from error
        return target

    def read(self, doc_id: str) -> Any | None:
        """读取一个文档，不存在时返回 ``None``。"""

        target = self.path_of(doc_id)
        with self._lock:
            if not os.path.isfile(target):
                return None
            try:
                with open(target, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, json.JSONDecodeError) as error:
                raise StorageError(f"读取文档 {doc_id} 失败: {error}", component="store") from error

    def remove(self, doc_id: str) -> bool:
        """删除一个文档，返回是否真的删掉了文件。"""

        target = self.path_of(doc_id)
        with self._lock:
            if not os.path.isfile(target):
                return False
            os.remove(target)
            return True

    def list_ids(self) -> list[str]:
        """列出全部文档标识（相对根目录、去扩展名、排序后）。"""

        with self._lock:
            found: list[str] = []
            for base, _dirs, files in os.walk(self._root):
                for name in files:
                    if not name.endswith(".json"):
                        continue
                    full = os.path.join(base, name)
                    relative = os.path.relpath(full, self._root)
                    found.append(relative[: -len(".json")].replace(os.sep, "/"))
        return sorted(found)
