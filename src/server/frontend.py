"""v4 前端构建产物的托管（`/next`）。

分工
----
* **构建**由 `web/`（Vite + Preact）负责，产物在 `web/dist/`，**不入库**；
* **托管**由本模块负责：`GET /next` 给出注入了会话令牌的 `index.html`，
  `GET /next/assets/*` 给出内容哈希命名的静态资源。

为什么自己写而不是 `StaticFiles`
--------------------------------
我们有两条**必须显式表达**的约束，`StaticFiles` 要么不做、要么不可见：

1. **令牌只注入 HTML**。资源文件是逐字节直出的 —— 一旦有谁把令牌注入逻辑放到
   静态资源路径上，令牌就会落到 `.js` / `.css` 里，然后被浏览器缓存到磁盘。
2. **`index.html` 不许被缓存**。它含本次进程的会话令牌；缓存住它等于把令牌写进磁盘。
   资源文件则相反：文件名带内容哈希，可以 `immutable` 长缓存。

路径穿越
--------
`{asset_path:path}` 会把 `../`、`%2e%2e%2f`、绝对路径原样交给我们，因此这里
**先 resolve 再判定是否仍在 assets 目录内**，越界一律 404（不区分「不存在」与「越界」，
免得把「外部有这个文件」这条信息漏出去）。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from . import errors

#: 构建产物目录名（在 `web/` 下）
DIST_DIRNAME = "dist"
#: 资源子目录名（Vite 的默认 `assetsDir`）
ASSETS_DIRNAME = "assets"

INDEX_FILENAME = "index.html"

#: 内容哈希命名的资源可以长期缓存；`index.html` 绝不缓存（含会话令牌）。
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
INDEX_CACHE_CONTROL = "no-store, no-cache, must-revalidate"

#: 强制内容类型，避免 Windows 注册表把 `.js` 猜成 `text/plain`。
_KNOWN_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


def dist_dir(web_dir: Path) -> Path:
    """`web/` → `web/dist`。"""
    return web_dir / DIST_DIRNAME


def assets_dir(web_dir: Path) -> Path:
    return dist_dir(web_dir) / ASSETS_DIRNAME


def index_path(web_dir: Path) -> Path:
    return dist_dir(web_dir) / INDEX_FILENAME


def is_built(web_dir: Path) -> bool:
    """构建产物是否就绪（`index.html` 在才算）。"""
    return index_path(web_dir).is_file()


def read_index(web_dir: Path) -> str:
    """读构建出来的 `index.html`；未构建时抛 `FRONTEND_NOT_BUILT`。"""
    path = index_path(web_dir)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise not_built_error() from exc
    except OSError as exc:
        raise errors.ToonTunerError(
            errors.FRONTEND_NOT_BUILT,
            f"前端构建产物无法读取（{exc.__class__.__name__}）。请重新执行 npm run build。",
        ) from exc


def not_built_error() -> errors.ToonTunerError:
    return errors.ToonTunerError(
        errors.FRONTEND_NOT_BUILT,
        "前端尚未构建，无法提供 /next 页面。",
        details={
            "steps": ["cd web", "npm ci", "npm run build"],
            "hint": "构建后 FastAPI 会自动托管 web/dist；开发时也可用 npm run dev 并让 /api 代理到本服务。",
        },
    )


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _KNOWN_TYPES:
        return _KNOWN_TYPES[suffix]
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def resolve_asset(web_dir: Path, asset_path: str) -> Path:
    """把 URL 里的资源路径解析成磁盘文件。

    越界（`../`、绝对路径、盘符）与「不存在」都返回 404 —— 不区分两者，
    因为「外部存在这个文件」本身就是不该泄漏的信息。
    """
    root = assets_dir(web_dir).resolve()
    relative = (asset_path or "").replace("\\", "/").lstrip("/")
    if not relative:
        raise asset_not_found(asset_path)

    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError) as exc:  # pragma: no cover - 极端路径形态
        raise asset_not_found(asset_path) from exc

    if not candidate.is_relative_to(root):
        raise asset_not_found(asset_path)
    if not candidate.is_file():
        raise asset_not_found(asset_path)
    return candidate


def asset_not_found(asset_path: str) -> errors.ToonTunerError:
    return errors.ToonTunerError(
        errors.ASSET_NOT_FOUND,
        "静态资源不存在。",
        details={"asset": str(asset_path)[-64:]},
    )
