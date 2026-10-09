"""本机会话令牌：所有**写接口**的统一闸门。

威胁模型（本项目只监听 127.0.0.1，因此防护目标是「同机上的其他程序 / 网页」）：

* 只监听回环并不能阻止同机其它进程、或用户浏览器里**任意网页**发起的跨站请求
  （浏览器的简单请求不发出预检，Form 提交也能打到 ``http://127.0.0.1:8765``）。
  一旦写成「取消参数校验」，后果是直接把用户的 ``.blend`` 写坏。
* 因此：进程启动时生成一个随机令牌，只有**由本服务返回的页面**才知道它；
  所有会改状态 / 写盘的接口都必须带上 ``X-Toon-Tuner-Token``。

三条硬约束：

1. **不写入静态文件**：``src/web/index.html`` 里只有占位注释，令牌由 ``GET /``
   在响应时注入，磁盘上永远不存在带令牌的文件。
2. **不写入日志**：本模块不打印令牌；比较用 ``hmac.compare_digest`` 做常量时间比较，
   错误信息里只说明「缺失 / 不匹配」，绝不回显令牌内容。
3. **默认拒绝**：如果守卫没有装配到 app 上（异常装配），一律按内部错误拒绝，
   绝不静默放行 —— 安全开关失效时必须朝「更严」的方向失败。
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Request

#: 写接口必须携带的请求头名
TOKEN_HEADER = "X-Toon-Tuner-Token"

#: 令牌随机字节数（urlsafe base64 后约 43 个字符）
TOKEN_BYTES = 32

#: index.html 里的注入占位注释；令牌只在此处被替换，静态文件本身不含令牌
TOKEN_PLACEHOLDER = "<!--TOON_TUNER_TOKEN-->"


def generate_session_token() -> str:
    """生成随机会话令牌（每次进程启动一份）。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


class SessionGuard:
    """令牌的唯一权威：持有令牌 + 常量时间比较。"""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or generate_session_token()

    @property
    def token(self) -> str:
        """当前令牌。

        ⚠ 只允许在两处使用：注入 ``GET /`` 的页面响应、以及测试断言。
        **不得**写入日志、错误响应或任何落盘文件。
        """
        return self._token

    def verify(self, provided: str | None) -> bool:
        if not provided:
            return False
        return hmac.compare_digest(str(provided), self._token)

    def reason(self, provided: str | None) -> str:
        """给用户看的失败原因，不含令牌内容。"""
        return "missing" if not provided else "mismatch"


async def require_session_token(request: Request) -> None:
    """FastAPI 依赖：校验写接口的会话令牌。

    以 ``Request`` 取 ``app.state.session_guard``，因此无需在每个路由里手工传参；
    路由上写 ``dependencies=[Depends(require_session_token)]`` 即可。
    """
    # 延迟导入，避免模块级循环依赖（errors 是纯常量模块，这里只是为了收窄导入面）
    from . import errors

    guard: SessionGuard | None = getattr(request.app.state, "session_guard", None)
    if guard is None:  # pragma: no cover - 装配错误，必须朝「更严」失败
        raise errors.ToonTunerError(
            errors.INTERNAL_ERROR, "会话令牌守卫未装配，写接口已拒绝执行。"
        )

    provided: str | None = request.headers.get(TOKEN_HEADER)
    if not guard.verify(provided):
        raise errors.ToonTunerError(
            errors.SESSION_TOKEN_INVALID,
            "写接口需要有效的本机会话令牌。",
            details={"header": TOKEN_HEADER, "reason": guard.reason(provided)},
        )


def inject_token(html: str, token: str) -> str:
    """把令牌注入页面响应。

    只替换占位注释；若占位缺失则退回「插到 ``</head>`` 之前」，
    再不行就追加到末尾 —— 保证页面永远拿得到令牌，避免写着写着前端静默失效。
    """
    import json as _json

    script = f"<script>window.__TOON_TUNER_TOKEN__ = {_json.dumps(token)};</script>"
    if TOKEN_PLACEHOLDER in html:
        return html.replace(TOKEN_PLACEHOLDER, script, 1)
    marker = "</head>"
    if marker in html:
        return html.replace(marker, script + "\n" + marker, 1)
    return html + script
