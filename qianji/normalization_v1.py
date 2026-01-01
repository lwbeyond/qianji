"""Frozen qianji-normalize/1 semantics, including its parsing helpers.

Keep this implementation available for archived publications. New normalization
semantics belong in a new version rather than changes to this module.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

MAX_BYTES = 128 * 1024 * 1024
KINDS = {"支出": "expense", "收入": "income", "转账": "transfer", "退款": "refund"}


class DataError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def record_id(ledger, key):
    return "qj_" + digest(canonical([ledger, key]))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError("JSON 存在重复字段，拒绝导入。")
        result[key] = value
    return result


def _bad_number(value):
    raise DataError("JSON 包含非有限数值。")


def json_safe(value):
    # Decimal values are preserved exactly as strings in internal comparison documents.
    # The original numeric JSON tokens remain in the byte-for-byte source archive.
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def parse_offset(value):
    if not isinstance(value, str) or not re.fullmatch(r"[+-](?:0[0-9]|1[0-4]):[0-5][0-9]", value):
        raise DataError("时区偏移必须是 ±HH:MM。")
    minutes = int(value[1:3]) * 60 + int(value[4:])
    if minutes > 14 * 60:
        raise DataError("时区偏移超出范围。")
    return timezone(timedelta(minutes=minutes if value[0] == "+" else -minutes))


def money(value):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise DataError("money 必须是 JSON 数值。")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount > Decimal("999999999999999"):
            raise DataError("金额必须是有限的非负数，且不超过支持范围。")
        if amount != amount.quantize(Decimal("0.01")):
            raise DataError("金额有超过两位的有效小数，拒绝静默舍入。")
        return format(abs(amount) if amount == 0 else amount, ".2f")
    except InvalidOperation as exc:
        raise DataError("金额精度超出支持范围。") from exc


def _text(row, key, *, required=False):
    value = row.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise DataError(f"{key} 必须是{'非空' if required else ''}字符串。")
    return value


def _asset(value):
    if value is None:
        return None
    if type(value) not in (int, str):
        raise DataError("账户编号必须是整数或字符串。")
    return str(value)


def normalize(row, ledger, utc_offset):
    if not isinstance(row, dict):
        raise DataError("每条账单必须是 JSON 对象。")
    key = _text(row, "key", required=True)
    source_type = _text(row, "type", required=True)
    if source_type not in KINDS:
        raise DataError("发现尚未支持的账单类型。")
    date_text = _text(row, "date", required=True)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", date_text):
        raise DataError("date 必须使用 YYYY-MM-DD HH:MM:SS 格式。")
    try:
        stamp = datetime.strptime(date_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=parse_offset(utc_offset))
    except ValueError as exc:
        raise DataError("账单日期无效。") from exc
    currency = _text(row, "currency", required=True)
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise DataError("currency 必须是三个大写字母。")
    amount = money(row.get("money"))
    source_id = _text(row, "sourceid") or None
    normalized = {
        "id": record_id(ledger, key), "ledger_id": ledger, "source_key": key,
        "occurred_at": stamp.isoformat(), "date_local": stamp.date().isoformat(),
        "source_date": date_text, "utc_offset": utc_offset,
        "timezone_basis": "configured_fixed_offset", "kind": KINDS[source_type],
        "amount": amount, "currency": currency,
        "category": _text(row, "category", required=True), "remark": _text(row, "remark"),
        "asset_id": _asset(row.get("asset")), "from_asset_id": _asset(row.get("from")),
        "to_asset_id": _asset(row.get("target")), "related_source_key": source_id,
        "related_record_id": record_id(ledger, source_id) if source_id else None,
        "source_type": source_type, "reimbursement_code": row.get("hasbx"),
    }
    if normalized["kind"] == "transfer" and (normalized["from_asset_id"] is None or normalized["to_asset_id"] is None):
        raise DataError("转账缺少转出或转入账户编号。")
    if row.get("hasbx") is not None and type(row["hasbx"]) is not int:
        raise DataError("hasbx 必须是整数或 null。")
    raw = json_safe(row)
    raw["money"] = amount
    return normalized, canonical(raw)
