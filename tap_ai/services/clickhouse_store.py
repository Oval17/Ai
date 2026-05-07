"""
Experimental ClickHouse-backed vector search.

This module mirrors the Pinecone vector-store API so we can benchmark both
backends side by side without changing the production router.
"""

from __future__ import annotations

import base64
import decimal
import json
import time
from datetime import date, datetime, time as dtime
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import frappe
from langchain_openai import OpenAIEmbeddings

from tap_ai.infra.config import get_config
from tap_ai.infra.sql_catalog import load_schema
from tap_ai.services.doctype_selector import pick_doctypes
from tap_ai.utils.remote_db import execute_remote_query


def _ch_base_url() -> str:
    host = get_config("clickhouse_host") or "127.0.0.1"
    port = get_config("clickhouse_port") or 8123
    return f"http://{host}:{port}"


def _ch_database() -> str:
    return get_config("clickhouse_database") or "default"


def _ch_table() -> str:
    return get_config("clickhouse_table") or "tap_ai_vector_store"


def _ch_auth_header() -> Optional[str]:
    user = get_config("clickhouse_user")
    if not user:
        return None

    password = get_config("clickhouse_password") or ""
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _emb() -> OpenAIEmbeddings:
    api_key = get_config("openai_api_key")
    model = get_config("embedding_model") or "text-embedding-3-small"
    if not api_key:
        raise RuntimeError("Missing openai_api_key in site_config.json")
    return OpenAIEmbeddings(model=model, api_key=api_key)


def _to_plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    return str(value)


def _record_to_text(doctype: str, row: Dict[str, Any]) -> str:
    parts: List[str] = []
    meta = frappe.get_meta(doctype)

    title_field = meta.title_field
    if title_field and row.get(title_field):
        label = meta.get_field(title_field).label or title_field
        parts.append(f"{label}: {row[title_field]}")

    parts.append(f"DocType: {doctype}")
    parts.append(f"ID: {row.get('name')}")

    for key, value in row.items():
        if key in ("name", title_field) or value in (None, ""):
            continue
        parts.append(f"{key}: {_to_plain(value)}")

    return "\n".join(parts)


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _request(sql: str, body: Optional[str] = None) -> str:
    url = f"{_ch_base_url()}/?{urlencode({'database': _ch_database(), 'query': sql})}"
    headers = {"Content-Type": "text/plain; charset=utf-8"}

    auth_header = _ch_auth_header()
    if auth_header:
        headers["Authorization"] = auth_header

    request = Request(url, data=body.encode("utf-8") if body is not None else None, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else str(exc)
        raise RuntimeError(f"ClickHouse request failed: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"ClickHouse connection failed: {exc}") from exc


def _execute_query(sql: str) -> List[Dict[str, Any]]:
    text = _request(f"{sql} FORMAT JSONEachRow")
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _execute_command(sql: str) -> None:
    _request(sql)


def _insert_json_each_row(table: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return

    body = "\n".join(json.dumps(row, ensure_ascii=False, default=_json_default) for row in rows)
    _request(f"INSERT INTO {table} FORMAT JSONEachRow", body=body)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    return str(value)


def _vector_literal(vector: List[float]) -> str:
    return "[" + ",".join(f"{float(value):.12f}" for value in vector) + "]"


def ensure_table() -> Dict[str, Any]:
    table = _ch_table()
    sql = f"""
CREATE TABLE IF NOT EXISTS {table} (
    doctype String,
    chunk_id String,
    record_ids Array(String),
    record_count UInt32,
    content String,
    metadata String,
    embedding Array(Float32),
    created_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(created_at)
ORDER BY (doctype, chunk_id)
""".strip()

    _execute_command(sql)
    return {"table": table, "database": _ch_database(), "ready": True}


def get_db_columns_for_doctype(doctype: str) -> List[str]:
    table = f"tab{doctype}"
    try:
        from tap_ai.utils.remote_db import get_remote_table_columns

        return get_remote_table_columns(doctype) or []
    except Exception:
        try:
            desc = frappe.db.sql(f"DESCRIBE `{table}`", as_dict=True)
            return [row["Field"] for row in desc]
        except Exception:
            return []


def _get_excluded_doctypes() -> set[str]:
    excluded: set[str] = set()
    try:
        recs = frappe.get_all("ExcludedDoctypes", fields=["name"], limit=1)
        if not recs:
            return excluded

        doc = frappe.get_doc("ExcludedDoctypes", recs[0].name)
        for row in doc.excluded_doctype:
            if row.doctype_name:
                excluded.add(row.doctype_name)
    except Exception:
        pass

    return excluded


def _filter_excluded(doctypes: List[str]) -> List[str]:
    excluded = _get_excluded_doctypes()
    return [doctype for doctype in doctypes if doctype not in excluded]


def _metadata_json(doctype: str, record_ids: List[str], count: int) -> str:
    return json.dumps({"doctype": doctype, "record_ids": record_ids, "count": count}, ensure_ascii=False)


def upsert_doctype(
    doctype: str,
    since: Optional[str] = None,
    group_records: int = 10,
    embed_batch: int = 10,
) -> Dict[str, Any]:
    ensure_table()
    emb = _emb()

    total_records = 0
    total_vectors = 0
    table = _ch_table()
    batch_rows: List[Dict[str, Any]] = []

    def flush() -> None:
        nonlocal total_vectors
        if not batch_rows:
            return

        vectors = emb.embed_documents([row["content"] for row in batch_rows])
        insert_rows = []
        for index, row in enumerate(batch_rows):
            insert_rows.append(
                {
                    "doctype": row["doctype"],
                    "chunk_id": row["chunk_id"],
                    "record_ids": row["record_ids"],
                    "record_count": row["record_count"],
                    "content": row["content"],
                    "metadata": row["metadata"],
                    "embedding": vectors[index],
                }
            )

        _insert_json_each_row(table, insert_rows)
        total_vectors += len(insert_rows)
        batch_rows.clear()

    try:
        query = f'SELECT * FROM "tab{doctype}" WHERE docstatus < 2'
        params: List[Any] = []
        if since:
            query += " AND modified >= %s"
            params.append(since)

        rows = execute_remote_query(query, tuple(params))
        group: List[Dict[str, Any]] = []

        for row in rows:
            total_records += 1
            group.append(row)

            if len(group) >= group_records:
                record_ids = [str(item["name"]) for item in group]
                content = "\n\n---\n\n".join(_record_to_text(doctype, item) for item in group)
                batch_rows.append(
                    {
                        "doctype": doctype,
                        "chunk_id": f"{doctype}:{record_ids[0]}",
                        "record_ids": record_ids,
                        "record_count": len(group),
                        "content": content,
                        "metadata": _metadata_json(doctype, record_ids, len(group)),
                    }
                )
                group = []

                if len(batch_rows) >= embed_batch:
                    flush()

        if group:
            record_ids = [str(item["name"]) for item in group]
            content = "\n\n---\n\n".join(_record_to_text(doctype, item) for item in group)
            batch_rows.append(
                {
                    "doctype": doctype,
                    "chunk_id": f"{doctype}:{record_ids[0]}",
                    "record_ids": record_ids,
                    "record_count": len(group),
                    "content": content,
                    "metadata": _metadata_json(doctype, record_ids, len(group)),
                }
            )

        flush()
    except Exception as exc:
        print(f"Error fetching remote data for {doctype}: {exc}")

    return {"doctype": doctype, "records_seen": total_records, "vectors_upserted": total_vectors}


def search_auto_namespaces(
    q: str,
    k: int = 8,
    route_top_n: int = 4,
    filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ensure_table()
    doctypes = pick_doctypes(q, top_n=route_top_n) or []
    doctypes = _filter_excluded(doctypes)

    if not doctypes:
        schema = load_schema()
        all_allowed = [table.replace("tab", "") for table in schema.get("allowlist", [])]
        content_priority = [
            "VideoClass",
            "Course",
            "LearningObjective",
            "NoteContent",
            "Quiz",
            "Assignment",
            "LearningUnit",
        ]
        doctypes = [doctype for doctype in content_priority if doctype in all_allowed][:route_top_n]
        if not doctypes:
            doctypes = all_allowed[:route_top_n]

    qvec = _emb().embed_query(q)
    vector_sql = _vector_literal(qvec)
    all_matches: List[Dict[str, Any]] = []

    for doctype in doctypes:
        try:
            where_sql = [f"doctype = {_sql_string(doctype)}"]

            if filters:
                for key, value in filters.items():
                    if value in (None, ""):
                        continue
                    where_sql.append(f"JSONExtractString(metadata, {_sql_string(key)}) = {_sql_string(str(value))}")

            query = f"""
SELECT
    chunk_id,
    doctype,
    record_ids,
    record_count,
    metadata,
    cosineDistance(embedding, {vector_sql}) AS score
FROM {_ch_table()}
WHERE {' AND '.join(where_sql)}
ORDER BY score ASC
LIMIT {int(k)}
""".strip()

            for row in _execute_query(query):
                all_matches.append(
                    {
                        "id": row.get("chunk_id"),
                        "score": row.get("score"),
                        "namespace": doctype,
                        "metadata": json.loads(row.get("metadata") or "{}"),
                    }
                )
        except Exception as exc:
            frappe.log_error(f"ClickHouse query failed for namespace {doctype}", str(exc))

    all_matches.sort(key=lambda item: item.get("score", 0), reverse=False)
    return {"q": q, "routed_doctypes": doctypes, "k": k, "matches": all_matches[:k]}


def cli_upsert_all(
    doctypes: Optional[List[str]] = None,
    since: Optional[str] = None,
) -> Dict[str, Any]:
    """bench execute tap_ai.services.clickhouse_store.cli_upsert_all"""

    if doctypes is None:
        schema = load_schema()
        doctypes = [table.replace("tab", "") for table in schema.get("allowlist", [])]

    total = len(doctypes)
    out: Dict[str, Any] = {}

    print(f"\n Starting ClickHouse upsert for {total} DocTypes...\n", flush=True)

    for index, doctype in enumerate(doctypes, 1):
        print(f"[{index}/{total}] ⏳ Processing: {doctype} ...", end="", flush=True)
        try:
            result = upsert_doctype(doctype, since=since)
            out[doctype] = result
            print(
                f"\r[{index}/{total}] ✅ {doctype:<30} records={result['records_seen']}, vectors={result['vectors_upserted']}",
                flush=True,
            )
        except Exception as exc:
            out[doctype] = {"error": str(exc)}
            print(f"\r[{index}/{total}] ❌ {doctype:<30} ERROR: {exc}", flush=True)
            frappe.log_error(f"ClickHouse upsert failed for {doctype}", str(exc))

    print(f"\n✅ Done. Processed {total} DocTypes.\n", flush=True)
    return out


def _timed_search(label: str, fn, repeat: int = 3) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    durations: List[float] = []

    for _ in range(repeat):
        start = time.perf_counter()
        out = fn()
        durations.append((time.perf_counter() - start) * 1000)
        results.append(out)

    return {
        "backend": label,
        "avg_ms": round(sum(durations) / len(durations), 2) if durations else 0.0,
        "min_ms": round(min(durations), 2) if durations else 0.0,
        "max_ms": round(max(durations), 2) if durations else 0.0,
        "runs": repeat,
        "sample_result": results[-1] if results else {},
    }


def cli_benchmark(q: str, k: int = 8, route_top_n: int = 4, repeat: int = 3) -> Dict[str, Any]:
    """
    Compare Pinecone vs ClickHouse on the same query.

    Example:
      bench execute tap_ai.services.clickhouse_store.cli_benchmark --kwargs "{'q':'summarize the video on financial literacy'}"
    """

    from tap_ai.services.pinecone_store import search_auto_namespaces as pinecone_search

    clickhouse_result = _timed_search(
        "clickhouse",
        lambda: search_auto_namespaces(q=q, k=k, route_top_n=route_top_n),
        repeat=repeat,
    )
    pinecone_result = _timed_search(
        "pinecone",
        lambda: pinecone_search(q=q, k=k, route_top_n=route_top_n),
        repeat=repeat,
    )

    comparison = {
        "query": q,
        "repeat": repeat,
        "k": k,
        "route_top_n": route_top_n,
        "pinecone": pinecone_result,
        "clickhouse": clickhouse_result,
        "faster_backend": "clickhouse" if clickhouse_result["avg_ms"] < pinecone_result["avg_ms"] else "pinecone",
    }
    print(frappe.as_json(comparison, indent=2))
    return comparison


def cli(q: str, k: int = 8, route_top_n: int = 4, repeat: int = 3):
    return cli_benchmark(q=q, k=k, route_top_n=route_top_n, repeat=repeat)