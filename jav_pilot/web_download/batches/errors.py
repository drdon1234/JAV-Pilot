"""Web download batch error types."""

from __future__ import annotations

from ..errors import WebDownloadError

AUTO_DISCOVERY_FAILURE_MESSAGES = {
    "challenge_active": "MissAV 挑战仍在处理中，后台重试后仍未恢复",
    "rate_limited": "MissAV 请求受到频率限制，后台重试后仍未恢复",
    "upstream_unavailable": "MissAV 上游暂时不可用，后台重试后仍未恢复",
    "navigation_timeout": "MissAV 页面加载超时，后台重试后仍未恢复",
    "transient_browser_failure": "MissAV 浏览器访问暂时受限，后台重试后仍未恢复",
    "dependency_unavailable": "MissAV 浏览器依赖当前不可用",
    "discovery_unavailable": "MissAV 资源发现当前不可用",
    "internal_failure": "MissAV 资源发现任务异常结束",
    "invalid_instruction": "MissAV 后台发现请求无效",
    "discovery_timeout": "MissAV 资源发现超时",
    "worker_exit": "MissAV 资源发现进程异常退出",
    "worker_protocol": "MissAV 资源发现进程返回了无效结果",
    "not_found": "MissAV 未找到对应作品",
    "parse_drift": "MissAV 页面结构暂时无法解析",
    "route_drift": "MissAV 作品地址暂时无法确认",
    "safety_rejected": "MissAV 资源未通过安全校验",
    "queue_failed": "MissAV 自动下载未能加入队列",
}


class WebDownloadBatchError(WebDownloadError):
    def __init__(self, message: str, *, failure_code: str = "queue_failed") -> None:
        super().__init__(message)
        self.failure_code = (
            failure_code
            if failure_code in AUTO_DISCOVERY_FAILURE_MESSAGES
            else "queue_failed"
        )


class WebDownloadBatchNotFoundError(WebDownloadBatchError):
    pass


class WebDownloadBatchConflictError(WebDownloadBatchError):
    pass


class WebDownloadBatchUnavailableError(WebDownloadBatchError):
    pass


class WebDownloadBatchTransientError(WebDownloadBatchError):
    pass
