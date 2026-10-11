"""
minihermes.errors —— 错误分类与重试策略

【架构】对应白皮书 01-Agent内核 §7「错误分类、重试与模型降级」。

核心思想：**不要用一个 try/except 兜所有错**。不同错误需要完全不同的应对：

    429 / 5xx / 网络抖动  → 退避重试（同一个模型再试一次就行）
    401 / 403             → 换凭证，或直接降级到备用模型
    400 + 上下文过长       → 压缩历史后重试
    其它 4xx              → 参数/请求本身有问题，重试没有意义，快速失败
"""

from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    RETRYABLE = "retryable"            # 服务端/网络临时故障
    RATE_LIMIT = "rate_limit"          # 限流
    AUTH = "auth"                      # 凭证问题
    CONTEXT_OVERFLOW = "context_overflow"
    BAD_REQUEST = "bad_request"
    FATAL = "fatal"                    # 未知错误


# 这些类型值得原地重试
_RETRYABLE = {ErrorKind.RETRYABLE, ErrorKind.RATE_LIMIT}

# 退避序列（秒）。真实系统会加抖动，这里保持可预测以便测试。
BACKOFF_SECONDS = [1.0, 2.0, 4.0]


def classify(exc: BaseException) -> ErrorKind:
    """把异常归类。判定顺序很重要：先具体后笼统，避免被 5xx 提前吃掉。"""
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()

    if status == 429 or "rate limit" in msg or "too many requests" in msg:
        return ErrorKind.RATE_LIMIT

    if status in (401, 403) or "unauthorized" in msg or "invalid api key" in msg:
        return ErrorKind.AUTH

    if status == 400 and any(
        k in msg
        for k in ("context length", "context_length", "too long", "maximum context", "tokens")
    ):
        return ErrorKind.CONTEXT_OVERFLOW

    if status is not None and status >= 500:
        return ErrorKind.RETRYABLE

    if status in (408, 409, 425):
        return ErrorKind.RETRYABLE

    if any(
        k in msg
        for k in ("timeout", "timed out", "connection", "网络", "temporarily", "unavailable", "reset")
    ):
        return ErrorKind.RETRYABLE

    if status is not None and 400 <= status < 500:
        return ErrorKind.BAD_REQUEST

    return ErrorKind.FATAL


def should_retry(kind: ErrorKind, attempt: int, max_attempts: int = 3) -> bool:
    """该错误是否还值得再试一次。attempt 从 1 开始。"""
    return kind in _RETRYABLE and attempt < max_attempts


def backoff_seconds(attempt: int) -> float:
    """第 attempt 次失败后的等待时长。"""
    idx = min(max(attempt - 1, 0), len(BACKOFF_SECONDS) - 1)
    return BACKOFF_SECONDS[idx]


def should_compress(kind: ErrorKind) -> bool:
    """这个错误是否应该通过压缩上下文来化解。"""
    return kind is ErrorKind.CONTEXT_OVERFLOW


def should_fallback(kind: ErrorKind) -> bool:
    """这个错误是否应该触发模型降级。"""
    return kind in {ErrorKind.AUTH, ErrorKind.FATAL}
