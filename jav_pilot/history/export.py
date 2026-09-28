"""History export as CSV or JSON records."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping
from datetime import UTC, datetime

from .candidates import parse_criteria, sql_filter
from .databases import asset_status_counts
from .errors import HistoryLifecycleValidationError
from .facts import assert_export_safe
from .fields import (
    canonical_bytes,
    history_variant_priority,
    history_web_variant,
    optional_int,
    optional_text,
    sql_slots,
    timestamp,
)
from .lifecycle_base import HistoryLifecycleBase
from .models import (
    HISTORY_EXPORT_REVISION,
    HISTORY_EXPORT_SCHEMA,
    MAX_EXPORT_RECORDS,
    Criteria,
    HistoryExport,
)


class HistoryExportMixin(HistoryLifecycleBase):
    def export_history(
        self,
        filters: Mapping[str, object] | None = None,
        *,
        format: object = "json",
    ) -> HistoryExport:
        criteria = parse_criteria(filters, maximum=MAX_EXPORT_RECORDS)
        clean_format = str(format or "json").strip().lower()
        if clean_format not in {"json", "csv"}:
            raise HistoryLifecycleValidationError("history export format is invalid")
        self._verify_database_identities()
        records = self._export_records(criteria)
        created_at = datetime.fromtimestamp(timestamp(self._clock()), UTC).isoformat()
        if clean_format == "json":
            unsigned = {
                "schema": HISTORY_EXPORT_SCHEMA,
                "revision": HISTORY_EXPORT_REVISION,
                "created_at": created_at,
                "record_count": len(records),
                "records": records,
            }
            checksum = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
            payload = {**unsigned, "checksum": f"sha256:{checksum}"}
            body = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            return HistoryExport(
                body,
                "application/json; charset=utf-8",
                "json",
                checksum,
                len(records),
            )

        output = io.StringIO(newline="")
        fieldnames = (
            "task_type",
            "id",
            "code",
            "variant",
            "status",
            "created_at",
            "updated_at",
            "details",
        )
        writer = csv.DictWriter(
            output, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    **record,
                    "details": json.dumps(
                        record.get("details", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
        csv_body = output.getvalue().encode("utf-8")
        checksum = hashlib.sha256(csv_body).hexdigest()
        metadata = (
            f"# schema={HISTORY_EXPORT_SCHEMA}\n"
            f"# revision={HISTORY_EXPORT_REVISION}\n"
            f"# created_at={created_at}\n"
            f"# checksum=sha256:{checksum}\n"
        ).encode("utf-8")
        return HistoryExport(
            metadata + csv_body,
            "text/csv; charset=utf-8",
            "csv",
            checksum,
            len(records),
        )

    def _export_records(self, criteria: Criteria) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for task_type in criteria.task_types:
            remaining = criteria.limit - len(records)
            if remaining <= 0:
                break
            if task_type == "batch":
                records.extend(self._export_batches(criteria, remaining))
            else:
                records.extend(self._export_jobs(task_type, criteria, remaining))
        records.sort(
            key=lambda item: (
                float(item["created_at"]),
                str(item["task_type"]),
                str(item["id"]),
            )
        )
        return records[: criteria.limit]

    def _export_jobs(
        self, task_type: str, criteria: Criteria, limit: int
    ) -> list[dict[str, object]]:
        path = self.web_path if task_type == "web" else self.metadata_path
        table = "web_download_jobs" if task_type == "web" else "jobs"
        clauses, values = sql_filter(criteria, task_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._readonly(path) as connection:
            rows = connection.execute(
                f"SELECT * FROM {table}{where} ORDER BY created_at, job_id LIMIT ?",
                (*values, limit),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            if task_type == "web":
                variant = history_web_variant(row["variant"])
                details = {
                    "requested_height": optional_int(row["requested_height"]),
                    "selected_height": optional_int(row["selected_height"]),
                    "verified_height": optional_int(row["verified_height"]),
                    "quality_strategy": str(row["quality_strategy"]),
                    "existing_policy": str(row["existing_policy"]),
                    "downloaded_bytes": int(row["downloaded_bytes"]),
                    "total_bytes": optional_int(row["total_bytes"]),
                    "publication_outcome": optional_text(row["publication_outcome"]),
                    "has_output": row["output_path"] is not None,
                    "replaces_history": row["replaces_job_id"] is not None,
                    "superseded_history": row["superseded_by_job_id"] is not None,
                }
            else:
                variant = None
                details = {
                    "kind": str(row["kind"]),
                    "attempts": int(row["attempts"]),
                    "has_media": row["relative_media_path"] is not None,
                    "assets": asset_status_counts(row["assets_json"]),
                }
            record = {
                "task_type": task_type,
                "id": str(row["job_id"]),
                "code": str(row["code"]),
                "variant": variant,
                "status": str(row["status"]),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
                "details": details,
            }
            assert_export_safe(record)
            result.append(record)
        return result

    def _export_batches(
        self, criteria: Criteria, limit: int
    ) -> list[dict[str, object]]:
        clauses, values = sql_filter(criteria, "batch", column_prefix="b.")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._readonly(self.web_path) as connection:
            rows = connection.execute(
                "SELECT b.*, COUNT(i.position) AS item_count "
                "FROM web_download_batches b "
                "LEFT JOIN web_download_batch_items i ON i.batch_id = b.batch_id"
                f"{where} GROUP BY b.batch_id "
                "ORDER BY b.created_at, b.batch_id LIMIT ?",
                (*values, limit),
            ).fetchall()
            batch_ids = tuple(str(row["batch_id"]) for row in rows)
            item_rows = (
                connection.execute(
                    "SELECT batch_id, position, code, variant "
                    "FROM web_download_batch_items "
                    f"WHERE batch_id IN ({sql_slots(batch_ids)}) "
                    "ORDER BY batch_id, position",
                    batch_ids,
                ).fetchall()
                if batch_ids
                else []
            )
        items_by_batch: dict[str, list[dict[str, object]]] = {
            batch_id: [] for batch_id in batch_ids
        }
        for item in item_rows:
            items_by_batch[str(item["batch_id"])].append(
                {
                    "position": int(item["position"]),
                    "code": str(item["code"]),
                    "variant": history_web_variant(item["variant"]),
                }
            )
        result = []
        for row in rows:
            batch_id = str(row["batch_id"])
            record = {
                "task_type": "batch",
                "id": batch_id,
                "code": str(row["code_or_prefix"]),
                "variant": None,
                "status": str(row["status"]),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
                "details": {
                    "root_chain_id": str(row["root_chain_id"]),
                    "page_number": int(row["page_number"]),
                    "mode": str(row["mode"]),
                    "max_height": int(row["max_height"]),
                    "existing_policy": str(row["existing_policy"]),
                    "variant_priority": list(
                        history_variant_priority(row["variant_priority_json"])
                    ),
                    "discovered_count": int(row["discovered_count"]),
                    "item_count": int(row["item_count"]),
                    "items": items_by_batch[batch_id],
                },
            }
            assert_export_safe(record)
            result.append(record)
        return result
