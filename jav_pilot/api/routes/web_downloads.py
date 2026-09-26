"""Web download queue and control endpoints."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus

from ...core.guards import QueryError
from ...core.observability import RUNTIME_METRICS
from ...downloads.replacements import DownloadReplacementError
from ...web_download.config import WebDownloadConfig
from ...web_download.errors import (
    WebDownloadConflictError,
    WebDownloadDisabledError,
    WebDownloadError,
)
from .. import state
from ..request import query_params, single_param, strict_int_param
from ..services.replacements import (
    attach_download_recovery,
    download_replacement_no_source_count,
    requires_web_download_restart,
    sanitized_bulk_retry_error,
    submit_web_replacement,
    with_web_intent_reselection,
    with_web_job_reselection,
)
from ..services.web_downloads import (
    require_web_download_site_available,
    web_download_batch_manager,
    web_download_manager,
    web_download_site_availability,
)
from .common import CommonErrorResponses

WEB_DOWNLOAD_BULK_RETRY_LIMIT = 500
WEB_DOWNLOAD_BULK_RETRY_MAX_LIMIT = 500
WEB_DOWNLOAD_BULK_RETRY_FAILURE_LIMIT = 25


class WebDownloadRoutes(CommonErrorResponses):
    def _handle_web_downloads(self, query_string: str) -> None:
        try:
            params = query_params(query_string)
            config = WebDownloadConfig.from_env()
            status_filter = single_param(params, "filter") or "all"
            code = single_param(params, "code") or None
            query = None if code is not None else (single_param(params, "q") or None)
            limit = strict_int_param(params, "limit", 100)
            offset = strict_int_param(params, "offset", 0)
            if limit < 1 or offset < 0 or offset > 10_000_000:
                raise QueryError("web download pagination is invalid")
            limit = min(limit, 500)
            if not config.enabled:
                self._send_json(
                    {
                        "ok": True,
                        "configured": False,
                        "enabled": False,
                        "available": False,
                        "reason": "Web downloads are disabled",
                        "tasks": [],
                        "intent": None,
                        "intents": [],
                        "failed_intent_count": 0,
                        "queued_intent_count": 0,
                        "count": 0,
                        "offset": offset,
                        "limit": limit,
                        "has_more": False,
                        "summary": {
                            "total": 0,
                            "running": 0,
                            "queued": 0,
                            "retrying": 0,
                            "completed": 0,
                            "missing": 0,
                            "failed": 0,
                            "speed": 0,
                        },
                        "max_concurrency": config.max_concurrency,
                        "no_source_failure_count": download_replacement_no_source_count(),
                    }
                )
                return
            web_available, unavailable_reason, providers = web_download_site_availability()
            manager = web_download_manager()
            tasks = manager.list(
                status_filter=status_filter,
                limit=limit,
                offset=offset,
                code=code,
                query=query,
            )
            tasks = attach_download_recovery(
                [with_web_job_reselection(task) for task in tasks]
            )
            count = manager.count(
                status_filter=status_filter,
                code=code,
                query=query,
            )
            summary = manager.summary()
            control = manager.control()
            batch_manager = web_download_batch_manager(config)
            failed_intent_count = (
                0
                if code is not None
                else batch_manager.count_auto_intents(status_filter="failed")
            )
            queued_intent_count = (
                0
                if code is not None
                else batch_manager.count_auto_intents(
                    status_filter="queued", query=query
                )
            )
            intent = (
                batch_manager.latest_auto_intent(code) if code is not None else None
            )
            if intent is not None:
                intent = with_web_intent_reselection(intent)
                intent = attach_download_recovery([intent])[0]
            intents = (
                []
                if code is not None or offset > 0
                else batch_manager.auto_intents(
                    status_filter=status_filter,
                    query=query,
                )
            )
            intents = [with_web_intent_reselection(item) for item in intents]
            intents = attach_download_recovery(intents)
            no_source_failure_count = download_replacement_no_source_count()
        except QueryError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json(
            {
                "ok": True,
                "configured": True,
                "enabled": web_available,
                "available": web_available,
                "reason": unavailable_reason,
                "providers": providers,
                "tasks": tasks,
                "intent": intent,
                "intents": intents,
                "failed_intent_count": failed_intent_count,
                "queued_intent_count": queued_intent_count,
                "count": count,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(tasks) < count,
                "summary": summary,
                "control": control,
                "max_concurrency": config.max_concurrency,
                "no_source_failure_count": no_source_failure_count,
            }
        )

    def _handle_web_download_control_get(self) -> None:
        try:
            control = web_download_manager().control()
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, "control": control})

    def _handle_web_download_control_update(self) -> None:
        try:
            payload = self._read_json_body(32 * 1024)
            action = payload.get("action")
            manager = web_download_manager()
            if action is not None:
                if set(payload) != {"action"}:
                    raise ValueError("web download control action is invalid")
                clean_action = str(action or "").strip().lower()
                if clean_action == "pause":
                    control = manager.global_pause()
                elif clean_action == "resume":
                    control = manager.global_resume()
                else:
                    raise ValueError("web download control action is invalid")
            else:
                allowed = {
                    "target_concurrency",
                    "bandwidth_limit",
                    "timezone",
                    "schedule",
                }
                if not payload or not set(payload).issubset(allowed):
                    raise ValueError("web download control update is invalid")
                control = manager.update_control(**payload)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, "control": control})

    def _handle_web_download_queue_update(self) -> None:
        try:
            payload = self._read_json_body(64 * 1024)
            action = str(payload.get("action") or "").strip().lower()
            manager = web_download_manager()
            if action == "priority":
                if not set(payload).issubset(
                    {"action", "job_id", "priority", "expected_revision"}
                ) or not {"action", "job_id", "priority"}.issubset(payload):
                    raise ValueError("web download priority update is invalid")
                job = manager.set_priority(
                    str(payload["job_id"]),
                    payload["priority"],
                    expected_revision=payload.get("expected_revision"),
                )
                control = manager.control()
                self._send_json({"ok": True, "job": job, "control": control})
                return
            if action == "reorder":
                if set(payload) != {"action", "job_ids", "expected_revision"}:
                    raise ValueError("web download queue reorder is invalid")
                job_ids = payload["job_ids"]
                if not isinstance(job_ids, list):
                    raise ValueError("web download queue reorder is invalid")
                control = manager.reorder(
                    job_ids,
                    expected_revision=payload["expected_revision"],
                )
            else:
                raise ValueError("web download queue action is invalid")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, "control": control})

    def _handle_web_download_start(self) -> None:
        try:
            payload = self._read_json_body(32 * 1024)
            allowed = {"code", "idempotency_key", "variant", "replacement_id"}
            if not set(payload).issubset(allowed) or not {
                "code",
                "idempotency_key",
            }.issubset(payload):
                raise ValueError("web download request is invalid")
            config = WebDownloadConfig.from_env()
            if not config.enabled:
                raise WebDownloadDisabledError("Web downloads are disabled")
            require_web_download_site_available()
            manager = web_download_manager(config)
            if payload.get("replacement_id") is not None:
                job = submit_web_replacement(
                    manager,
                    code=payload.get("code"),
                    idempotency_key=payload.get("idempotency_key"),
                    replacement_id=payload.get("replacement_id"),
                    variant=payload.get("variant"),
                )
            else:
                if "variant" in payload:
                    job = manager.start(
                        payload.get("code"),
                        payload.get("idempotency_key"),
                        variant=payload.get("variant"),
                    )
                else:
                    job = manager.start(
                        payload.get("code"),
                        payload.get("idempotency_key"),
                    )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except DownloadReplacementError as exc:
            self._send_download_replacement_error(exc)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, "job": job}, HTTPStatus.ACCEPTED)

    def _handle_web_download_action(self) -> None:
        try:
            payload = self._read_json_body(32 * 1024)
            job_id = str(payload.get("job_id") or "").strip()
            action = str(payload.get("action") or "").strip()
            if not job_id:
                raise ValueError("job_id is required")
            if action.lower() in {"retry", "restart"}:
                require_web_download_site_available()
            result = web_download_manager().action(job_id, action)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        if action.lower() in {"retry", "restart"}:
            RUNTIME_METRICS.record_retry_action()
        self._send_json({"ok": True, "job": result})

    def _handle_web_download_retry_failed(self) -> None:
        acquired = False
        try:
            payload = self._read_json_body(4096)
            if not set(payload).issubset({"limit"}):
                raise ValueError("web download bulk retry request is invalid")
            raw_limit = payload.get("limit", WEB_DOWNLOAD_BULK_RETRY_LIMIT)
            if (
                type(raw_limit) is not int
                or not 1 <= raw_limit <= WEB_DOWNLOAD_BULK_RETRY_MAX_LIMIT
            ):
                raise ValueError("web download bulk retry limit is invalid")
            require_web_download_site_available()
            acquired = state.WEB_DOWNLOAD_BULK_RETRY_LOCK.acquire(blocking=False)
            if not acquired:
                raise WebDownloadConflictError(
                    "another web download bulk retry is already running"
                )
            manager = web_download_manager()
            batch_manager = web_download_batch_manager()
            jobs = manager.list(status_filter="failed", limit=raw_limit)
            intents = batch_manager.auto_intents(
                status_filter="failed", limit=raw_limit
            )
            total_candidates = manager.count(
                status_filter="failed"
            ) + batch_manager.count_auto_intents(status_filter="failed")
            candidates = [
                {
                    "source_kind": "web_job",
                    "source_id": str(job.get("job_id") or ""),
                    "code": str(job.get("code") or ""),
                    "created_at": float(job.get("created_at") or 0),
                    "error": job.get("error"),
                }
                for job in jobs
            ] + [
                {
                    "source_kind": "web_intent",
                    "source_id": str(intent.get("batch_id") or ""),
                    "code": str(intent.get("code_or_prefix") or ""),
                    "created_at": float(intent.get("created_at") or 0),
                    "error": intent.get("error"),
                }
                for intent in intents
            ]
            candidates.sort(
                key=lambda item: (
                    float(item["created_at"]),
                    str(item["source_kind"]),
                    str(item["source_id"]),
                ),
                reverse=True,
            )
            selected = candidates[:raw_limit]
            truncated = total_candidates > len(selected)
            job_retried = 0
            intent_retried = 0
            job_failed = 0
            intent_failed = 0
            failures: list[dict[str, object]] = []
            for candidate in selected:
                source_kind = str(candidate["source_kind"])
                source_id = str(candidate["source_id"])
                try:
                    if source_kind == "web_job":
                        if requires_web_download_restart(candidate.get("error")):
                            manager.restart(source_id)
                        else:
                            manager.retry(source_id)
                        job_retried += 1
                    else:
                        batch_manager.action(source_id, "retry", None)
                        intent_retried += 1
                    RUNTIME_METRICS.record_retry_action()
                except (WebDownloadError, OSError, sqlite3.Error) as exc:
                    if source_kind == "web_job":
                        job_failed += 1
                    else:
                        intent_failed += 1
                    if len(failures) < WEB_DOWNLOAD_BULK_RETRY_FAILURE_LIMIT:
                        failures.append(
                            {
                                "source_kind": source_kind,
                                "source_id": source_id,
                                "code": str(candidate["code"]),
                                "error": sanitized_bulk_retry_error(exc),
                            }
                        )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        finally:
            if acquired:
                state.WEB_DOWNLOAD_BULK_RETRY_LOCK.release()
        self._send_json(
            {
                "ok": True,
                "summary": {
                    "job_retried": job_retried,
                    "intent_retried": intent_retried,
                    "job_failed": job_failed,
                    "intent_failed": intent_failed,
                    "total_candidates": total_candidates,
                    "limit": raw_limit,
                    "truncated": truncated,
                },
                "failures": failures,
            }
        )

    def _handle_web_download_cleanup_missing(self) -> None:
        try:
            result = web_download_manager().cleanup_missing()
        except WebDownloadError as exc:
            self._send_web_download_error(exc)
            return
        except (OSError, sqlite3.Error):
            self._send_web_download_unavailable()
            return
        self._send_json({"ok": True, **result})
