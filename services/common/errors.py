"""统一错误码与异常。

契约见 `docs/DESIGN-API.md` 第 0.2 / 0.3 节。

关键设计：**权限不足与"不存在"返回同一个错误码**（fail-closed）。
不能让调用方通过错误码差异探测资源是否存在 —— 那是租户隔离的信息泄漏。
"""

from __future__ import annotations

from typing import Any


class PkhError(Exception):
    """所有业务异常的基类。

    子类定义 `code` 与 `status_code`，由 FastAPI 异常处理器统一转成契约格式。
    """

    code: str = "INTERNAL"
    status_code: int = 500
    message: str = "服务端错误"

    def __init__(self, message: str | None = None, detail: Any = None,
                 headers: dict[str, str] | None = None) -> None:
        self.message = message or self.message
        self.detail = detail
        #: 🔑 协议要求随错误返回的头（目前只有 `Retry-After`，D135）。
        #: ⚠️ **不用** detail 夹带 —— `detail` 是 JSON 体里的字段，
        #:   而 `Retry-After` 是 HTTP 头，客户端按标准只认头。
        self.headers = headers or {}
        super().__init__(self.message)

    def to_dict(self, request_id: str | None = None) -> dict:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            body["detail"] = self.detail
        if request_id:
            body["request_id"] = request_id
        return {"error": body}


# ---------------- 400 ----------------


class InvalidRequest(PkhError):
    code = "INVALID_REQUEST"
    status_code = 400
    message = "请求参数缺失或格式错误"


# ---------------- 401 ----------------


class InvalidToken(PkhError):
    code = "INVALID_TOKEN"
    status_code = 401
    message = "API Key 缺失或无效"


# ---------------- 403 / 404 ----------------
#
# 注意：两者共用同一个 code（KB_NOT_FOUND），这是 D8 fail-closed 的要求。
# 分成两个类只是为了在服务端日志里能区分原因（detail 里写内部原因，
# 但 code 对外一致）。


class ResourceNotFound(PkhError):
    """资源不存在**或**调用方无权限 —— 对外表现完全一致。"""

    code = "KB_NOT_FOUND"
    status_code = 403
    message = "资源不存在或无访问权限"


class DocNotFound(PkhError):
    code = "DOC_NOT_FOUND"
    status_code = 404
    message = "文档不存在或无访问权限"


# ---------------- 413 / 415 / 422 ----------------


class FileTooLarge(PkhError):
    code = "FILE_TOO_LARGE"
    status_code = 413
    message = "文件超过大小上限"


class UnsupportedFormat(PkhError):
    code = "UNSUPPORTED_FORMAT"
    status_code = 415
    message = "不支持的文件格式"


class ValidationFailed(PkhError):
    code = "VALIDATION_FAILED"
    status_code = 422
    message = "校验失败"


# ---------------- 403 ----------------


class RegistrationClosed(PkhError):
    """自助注册已关闭（D138 · `allow_register=false`）。

    🔴 **不要**复用 `ResourceNotFound`：那个的 code 是 `KB_NOT_FOUND`，
    语义是"这个资源对你不存在"，用它回答"注册关了"会让调用方**分不清**
    "关着"和"你看不到"—— 而这两种情况的正确反应完全不同。
    """

    code = "REGISTRATION_CLOSED"
    status_code = 403
    message = "本实例已关闭自助注册"


class Forbidden(PkhError):
    """操作级拒绝（**不是**"资源不存在"）。

    ⚠️ 与 `ResourceNotFound` 的区别：后者用于"访问某个资源"（fail-closed，
    不区分不存在与没权限）；这里是"这个操作你就不能做"（如改全局 LLM 配置），
    没有"资源存不存在"可探测 —— 所以给一个明确的 403 更清楚。
    """

    code = "FORBIDDEN"
    status_code = 403
    message = "没有执行该操作的权限"


# ---------------- 429 ----------------


class RateLimited(PkhError):
    code = "RATE_LIMITED"
    status_code = 429
    message = "超出配额"


# ---------------- 5xx ----------------


class ModelUnavailable(PkhError):
    code = "MODEL_UNAVAILABLE"
    status_code = 503
    message = "模型未就绪"


class InternalError(PkhError):
    code = "INTERNAL"
    status_code = 500
    message = "服务端错误"
