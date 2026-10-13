"""模型注册表：扫描受信任的模型目录，生成私有指纹与 opaque model_id。

安全要点（规格 §11）：
* 前端只拿到 ``model-{fp[:16]}`` 这种**不暴露真实路径**的 ID。
* 真实路径只存在于服务端私有注册表（``.model_registry.json``，不入库、不出现在任何 API 响应）。
* 调用方只能凭 model_id 提交渲染，绝不允许提交绝对路径或文件名。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 受信任模型目录中允许扫描的扩展名。
_MODEL_EXTS = (".pmx", ".pmd", ".blend", ".x")

#: 指纹 = 文件大小 + 前 1MB 内容的 sha256，避免读整支大文件。
_FP_SAMPLE = 1 << 20


def fingerprint_of(path: Path) -> str:
    h = hashlib.sha256()
    try:
        h.update(str(path.stat().st_size).encode("utf-8"))
    except OSError:
        pass
    with open(path, "rb") as f:
        h.update(f.read(_FP_SAMPLE))
    return h.hexdigest()


def model_id_from_fingerprint(fp: str) -> str:
    return "model-" + fp[:16]


@dataclass
class _ModelEntry:
    model_id: str
    fingerprint: str
    name: str
    path: str
    material_count: int = 0
    last_job_id: str | None = None


class ModelRegistry:
    """扫描 model_dir，维护 model_id <-> 真实路径的私有映射。"""

    def __init__(self, model_dir: str | os.PathLike | None, registry_path: str | os.PathLike | None = None) -> None:
        self._model_dir = Path(model_dir) if model_dir else None
        if registry_path is None:
            registry_path = Path.home() / ".toon_model_registry.json"
        self._registry_path = Path(registry_path)
        self._by_fp: dict[str, _ModelEntry] = {}
        self._by_id: dict[str, _ModelEntry] = {}
        self._load()

    # -- 持久化（含真实路径，绝不上传、不进仓库） -------------------------
    def _load(self) -> None:
        if not self._registry_path.is_file():
            return
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in data.get("models", []):
            fp = item.get("fingerprint")
            if not fp:
                continue
            entry = _ModelEntry(
                model_id=item.get("model_id", model_id_from_fingerprint(fp)),
                fingerprint=fp,
                name=item.get("name", ""),
                path=item.get("path", ""),
                material_count=item.get("material_count", 0),
                last_job_id=item.get("last_job_id"),
            )
            self._by_fp[fp] = entry
            self._by_id[entry.model_id] = entry

    def _persist(self) -> None:
        data = {
            "models": [
                {
                    "model_id": e.model_id,
                    "fingerprint": e.fingerprint,
                    "name": e.name,
                    "path": e.path,
                    "material_count": e.material_count,
                    "last_job_id": e.last_job_id,
                }
                for e in self._by_fp.values()
            ]
        }
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._registry_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # -- 扫描 ----------------------------------------------------------
    def scan(self) -> list[dict[str, Any]]:
        """重新扫描受信任目录。找不到目录时返回空列表（不抛错）。"""
        if not self._model_dir or not self._model_dir.is_dir():
            return []
        found: list[_ModelEntry] = []
        for root, _dirs, files in os.walk(self._model_dir):
            for fn in sorted(files):
                if not fn.lower().endswith(_MODEL_EXTS):
                    continue
                p = Path(root) / fn
                try:
                    fp = fingerprint_of(p)
                except OSError:
                    continue
                mid = model_id_from_fingerprint(fp)
                entry = self._by_fp.get(fp) or _ModelEntry(mid, fp, p.stem, str(p))
                entry.model_id = mid
                entry.fingerprint = fp
                entry.name = p.stem
                entry.path = str(p)
                self._by_fp[fp] = entry
                self._by_id[mid] = entry
                found.append(entry)
        self._persist()
        return [self._public(e) for e in found]

    # -- 查询（内部用，返回私有信息） -----------------------------------
    def find(self, model_id: str) -> _ModelEntry | None:
        return self._by_id.get(model_id)

    def resolve(self, model_id: str) -> tuple[str, str, str]:
        """解析 model_id -> (真实路径, 指纹, 名称)。找不到抛 KeyError。"""
        e = self._by_id.get(model_id)
        if e is None:
            raise KeyError(model_id)
        return e.path, e.fingerprint, e.name

    def set_material_count(self, model_id: str, count: int) -> None:
        e = self._by_id.get(model_id)
        if e is not None:
            e.material_count = count
            self._persist()

    def remember_last_job(self, model_id: str, job_id: str) -> None:
        e = self._by_id.get(model_id)
        if e is not None:
            e.last_job_id = job_id
            self._persist()

    # -- 对外（绝不泄漏真实路径） ---------------------------------------
    def _public(self, e: _ModelEntry) -> dict[str, Any]:
        return {
            "model_id": e.model_id,
            "name": e.name,
            "fingerprint": e.fingerprint[:16],
            "material_count": e.material_count,
        }

    def to_public(self) -> list[dict[str, Any]]:
        return [self._public(e) for e in self._by_id.values()]
