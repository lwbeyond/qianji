"""Standalone standard-library client; can be copied into a consumer project.

Use read_all() for an atomic consumer read: an interrupted/changing source raises,
instead of returning a partial dataset that could be mistaken for complete data.
"""

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class QianjiAPIError(RuntimeError):
    pass


class QianjiClient:
    def __init__(self, base_url="http://127.0.0.1:8790", *, timeout=15):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout
        response = self._get("/api/v1/snapshot")
        if response.get("api_version") != "1.0":
            raise QianjiAPIError("Unsupported API version")
        self.snapshot = response.get("snapshot")
        _validate_snapshot_metadata(self.snapshot)

    def _get(self, path, params=None):
        url = self.base_url + path + ("?" + urlencode(params) if params else "")
        try:
            with urlopen(Request(url, headers={"Accept": "application/json", "X-Qianji-Contract-Version": "1.1"}),
                         timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                code = json.loads(exc.read())["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = "http_error"
            finally:
                exc.close()
            raise QianjiAPIError(f"{code} (HTTP {exc.code}); restart the read if the snapshot changed") from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise QianjiAPIError("Local Qianji API unavailable or invalid response") from exc

    def read_all(self, dataset="transactions", *, page_size=500, **filters):
        if dataset not in self.snapshot["available_datasets"]:
            raise QianjiAPIError("Unknown dataset")
        if set(filters) & {"generation", "limit", "offset"}:
            raise ValueError("Use page_size; create a new client to refresh the snapshot")
        rows, offset, total = [], 0, None
        while True:
            result = self._get("/api/v1/datasets/" + dataset,
                               {**filters, "generation": self.snapshot["generation"],
                                "limit": page_size, "offset": offset})
            if result.get("snapshot") != self.snapshot or result.get("dataset") != dataset:
                raise QianjiAPIError("Snapshot or dataset mismatch")
            page, data = result["pagination"], result["data"]
            if (page["offset"] != offset or page["limit"] != page_size or page["returned"] != len(data)
                    or len(data) > page_size or (total is not None and page["total"] != total)):
                raise QianjiAPIError("Invalid pagination")
            total = page["total"]
            rows.extend(data)
            following = page["next_offset"]
            if following is None:
                if len(rows) != total:
                    raise QianjiAPIError("Incomplete dataset")
                return rows
            if type(following) is not int or following != offset + len(data) or following <= offset:
                raise QianjiAPIError("Pagination did not advance")
            offset = following

    def read_batch(self, requests, *, page_size=500):
        """Collect every requested table before exposing a result; a 409 aborts all."""
        if not isinstance(requests, dict) or not requests:
            raise ValueError("Specify a dataset/filter mapping")
        datasets = {}
        for name, filters in requests.items():
            rows = self.read_all(name, page_size=page_size, **filters)
            _validate(rows, snapshot_schema()["properties"]["datasets"]["properties"][name])
            datasets[name] = rows
        return {"format": "qianji-query", "snapshot": self.snapshot,
                "queries": requests, "datasets": datasets}


def load_snapshot(path, *, expected_sha256=None, expected_library_id=None):
    """Read the atomically published file without running an HTTP service."""
    content = Path(path).read_bytes()
    if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
        raise QianjiAPIError("Publication checksum mismatch")
    try:
        doc = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise QianjiAPIError("Invalid snapshot JSON") from exc
    return validate_snapshot(doc, expected_library_id=expected_library_id)


def validate_snapshot(doc, *, expected_library_id=None):
    """Validate a complete in-memory publication before exposing or publishing it.

    This is deliberately independent of producer code so a copied client checks
    structural, provenance and accounting invariants with the same contract.
    """
    _validate(doc, snapshot_schema())
    _validate_snapshot_metadata(doc["snapshot"])
    if expected_library_id is not None and doc["snapshot"]["library_id"] != expected_library_id:
        raise QianjiAPIError("Library identity changed; consume a complete new library")
    if doc.get("format") != "qianji-normalized" or doc.get("format_version") != 1:
        raise QianjiAPIError("Unsupported snapshot format")
    payload = json.dumps(doc["datasets"], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if hashlib.sha256(payload).hexdigest() != doc["snapshot"]["data_sha256"]:
        raise QianjiAPIError("Snapshot checksum mismatch")
    if {key: len(rows) for key, rows in doc["datasets"].items()} != doc["snapshot"]["counts"]:
        raise QianjiAPIError("Snapshot count mismatch")
    _validate_datasets(doc)
    return doc


def _offset(value):
    if not re.fullmatch(r"[+-](?:0[0-9]|1[0-4]):[0-5][0-9]", value):
        raise QianjiAPIError("Invalid ledger UTC offset")
    minutes = int(value[1:3]) * 60 + int(value[4:])
    if minutes > 14 * 60:
        raise QianjiAPIError("Invalid ledger UTC offset")
    return timedelta(minutes=minutes if value[0] == "+" else -minutes)


def _validate_snapshot_metadata(snapshot):
    _validate(snapshot, snapshot_schema()["properties"]["snapshot"])
    if snapshot["generation"] != snapshot["library_id"] + ":" + str(snapshot["revision"]):
        raise QianjiAPIError("Generation identity mismatch")
    ledgers = set()
    for ledger in snapshot["ledgers"]:
        if not ledger["id"].strip() or ledger["id"] in ledgers:
            raise QianjiAPIError("Invalid or duplicate ledger identity")
        ledgers.add(ledger["id"])
        _offset(ledger["utc_offset"])
    if "processing" in snapshot:
        processing = snapshot["processing"]
        if processing["source_revision"] > snapshot["revision"]:
            raise QianjiAPIError("Source revision exceeds publication revision")
        try:
            source_time = datetime.fromisoformat(processing["source_updated_at"].replace("Z", "+00:00"))
            processed_time = datetime.fromisoformat(processing["processed_at"].replace("Z", "+00:00"))
            if source_time > processed_time:
                raise QianjiAPIError("Processing predates source update")
        except (TypeError, ValueError) as exc:
            raise QianjiAPIError("Invalid processing timestamp") from exc


def _record_id(ledger, key):
    identity = json.dumps([ledger, key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "qj_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _validate_datasets(doc):
    ledgers = {ledger["id"]: ledger["utc_offset"] for ledger in doc["snapshot"]["ledgers"]}
    source_revision = doc["snapshot"].get("processing", {}).get("source_revision", doc["snapshot"]["revision"])
    rows = doc["datasets"]["transactions"]
    ids = set()
    kinds = {"支出": "expense", "收入": "income", "退款": "refund", "转账": "transfer"}
    groups = {}
    # Decimal arithmetic must remain exact even for totals exceeding the default
    # context precision. Amount strings have already passed the machine schema.
    precision = max((len(row["amount"]) for row in rows), default=1) + len(str(len(rows))) + 4
    with localcontext() as context:
        context.prec = max(28, precision)
        for row in rows:
            ledger, key = row["ledger_id"], row["source_key"]
            if (not key.strip() or row["id"] != _record_id(ledger, key) or row["id"] in ids):
                raise QianjiAPIError("Invalid or duplicate transaction identity")
            ids.add(row["id"])
            if row["revision"] > source_revision:
                raise QianjiAPIError("Transaction revision exceeds source revision")
            if ledger not in ledgers or row["utc_offset"] != ledgers[ledger]:
                raise QianjiAPIError("Transaction ledger or UTC offset mismatch")
            occurred = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
            if (occurred.utcoffset() != _offset(row["utc_offset"])
                    or occurred.date().isoformat() != row["date_local"]):
                raise QianjiAPIError("Transaction date or UTC offset mismatch")
            if row["kind"] != kinds[row["source_type"]]:
                raise QianjiAPIError("Transaction source type mismatch")
            related = row["related_source_key"]
            expected_related = _record_id(ledger, related) if related else None
            if row["related_record_id"] != expected_related:
                raise QianjiAPIError("Related transaction identity mismatch")
            group_key = ledger, row["date_local"], row["currency"]
            group = groups.setdefault(group_key, {"ledger_id": ledger, "date_local": row["date_local"],
                "currency": row["currency"], "count": 0,
                **{kind: Decimal(0) for kind in kinds.values()}})
            group["count"] += 1
            group[row["kind"]] += Decimal(row["amount"])
        expected = {}
        for key, group in groups.items():
            group["net_expense"] = group["expense"] - group["refund"]
            group["net_cashflow"] = group["income"] + group["refund"] - group["expense"]
            expected[key] = {name: format(value, ".2f") if isinstance(value, Decimal) else value
                             for name, value in group.items()}
    actual = {}
    for row in doc["datasets"]["daily_summary"]:
        key = row["ledger_id"], row["date_local"], row["currency"]
        if key in actual:
            raise QianjiAPIError("Duplicate daily summary identity")
        actual[key] = row
    if actual != expected:
        raise QianjiAPIError("Daily summary does not match transactions")


def snapshot_schema():
    """Machine contract authority; generated snapshot.schema.json must match this."""
    def obj(properties):
        return {"type": "object", "required": list(properties), "properties": properties,
                "additionalProperties": False}

    def array(item):
        return {"type": "array", "items": item}

    text = {"type": "string"}
    nullable = {"type": ["string", "null"]}
    sha = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    money = {"type": "string", "pattern": "^(0|[1-9][0-9]*)\\.[0-9]{2}$"}
    signed = {"type": "string", "pattern": "^-?(0|[1-9][0-9]*)\\.[0-9]{2}$"}
    count = {"type": "integer", "minimum": 0}
    day = {"type": "string", "format": "date"}
    stamp = {"type": "string", "format": "date-time"}
    version = {"type": "string", "pattern": r"[\s\S]*\S[\s\S]*"}
    currency = {"type": "string", "pattern": "^[A-Z]{3}$"}
    offset = {"type": "string", "pattern": "^[+-][0-9]{2}:[0-9]{2}$"}
    transaction = obj({
        "id": {"type": "string", "pattern": "^qj_[a-f0-9]{64}$"},
        "ledger_id": text, "source_key": text, "revision": {"type": "integer", "minimum": 1},
        "occurred_at": stamp, "date_local": day, "source_date": text, "utc_offset": offset,
        "timezone_basis": {"const": "configured_fixed_offset"},
        "kind": {"enum": ["expense", "income", "refund", "transfer"]},
        "source_type": {"enum": ["支出", "收入", "退款", "转账"]},
        "amount": money, "currency": currency, "category": text, "remark": nullable,
        "asset_id": nullable, "from_asset_id": nullable, "to_asset_id": nullable,
        "related_source_key": nullable, "related_record_id": nullable,
        "reimbursement_code": {"type": ["integer", "null"]},
        "source_file_sha256": sha, "source_row": {"type": "integer", "minimum": 1},
    })
    summary = obj({"ledger_id": text, "date_local": day, "currency": currency, "count": count,
                   **{k: money for k in ("expense", "income", "refund", "transfer")},
                   "net_expense": signed, "net_cashflow": signed})
    result = obj({"format": {"const": "qianji-normalized"}, "format_version": {"const": 1},
                  "snapshot": obj({"generation": text, "library_id": {"type": "string", "format": "uuid"},
                                   "revision": count, "updated_at": stamp, "contract_version": {"enum": ["1.0", "1.1"]},
                                   "processing": obj({"normalizer_version": version, "summary_version": version,
                                                      "source_revision": count, "source_updated_at": stamp,
                                                      "processed_at": stamp}),
                                   "ledgers": array(obj({"id": text, "utc_offset": offset})),
                                   "available_datasets": {"const": ["transactions", "daily_summary"]},
                                   "data_sha256": sha, "counts": obj({"transactions": count, "daily_summary": count})}),
                  "datasets": obj({"transactions": array(transaction), "daily_summary": array(summary)})})
    snapshot = result["properties"]["snapshot"]
    snapshot["required"].remove("processing")
    snapshot["if"] = {"properties": {"contract_version": {"const": "1.1"}}}
    snapshot["then"] = {"required": ["processing"]}
    snapshot["else"] = {"not": {"required": ["processing"]}}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **result}


def _validate(value, schema):
    if "if" in schema:
        try:
            _validate(value, schema["if"])
        except QianjiAPIError:
            _validate(value, schema.get("else", {}))
        else:
            _validate(value, schema.get("then", {}))
    if "not" in schema:
        try:
            _validate(value, schema["not"])
        except QianjiAPIError:
            pass
        else:
            raise QianjiAPIError("Machine schema prohibited fields mismatch")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "null": value is None,
               "integer": type(value) is int}
    if types and not any(matches[t] for t in types):
        raise QianjiAPIError("Machine schema type mismatch")
    if "const" in schema and (value != schema["const"] or isinstance(value, bool)):
        raise QianjiAPIError("Machine schema constant mismatch")
    if "enum" in schema and value not in schema["enum"]:
        raise QianjiAPIError("Machine schema enum mismatch")
    if "minimum" in schema and value < schema["minimum"]:
        raise QianjiAPIError("Machine schema range mismatch")
    if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        raise QianjiAPIError("Machine schema pattern mismatch")
    try:
        if schema.get("format") == "date":
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("Noncanonical date")
        elif schema.get("format") == "date-time":
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                                r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value):
                raise ValueError("Invalid date-time")
            if datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is None:
                raise ValueError("Timezone required")
        elif schema.get("format") == "uuid":
            from uuid import UUID
            UUID(value)
    except (TypeError, ValueError) as exc:
        raise QianjiAPIError("Machine schema time or identity mismatch") from exc
    if isinstance(value, dict):
        if not set(schema.get("required", [])) <= value.keys():
            raise QianjiAPIError("Machine schema fields mismatch")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise QianjiAPIError("Machine schema fields mismatch")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key])
    if isinstance(value, list) and "items" in schema:
        for item in value:
            _validate(item, schema["items"])
