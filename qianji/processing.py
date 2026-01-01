"""Versioned deterministic processing; archived publications pin these versions."""

from decimal import Decimal

from .normalization_v1 import DataError, normalize as normalize_v1

NORMALIZER_VERSION = 'qianji-normalize/1'
SUMMARY_VERSION = 'qianji-summary/1'
LEGACY_NORMALIZER_VERSION = 'qianji-normalize/1'
LEGACY_SUMMARY_VERSION = 'qianji-summary/1'


def summarize_v1(rows):
    """Frozen qianji-summary/1: exact amounts, separate ledger/day/currency."""
    groups = {}
    for row in rows:
        key = row["ledger_id"], row["date_local"], row["currency"]
        group = groups.setdefault(key, {"ledger_id": key[0], "date_local": key[1], "currency": key[2],
                                        "count": 0, **{k: Decimal(0) for k in ("expense", "income", "refund", "transfer")}})
        group["count"] += 1
        group[row["kind"]] += Decimal(row["amount"])
    result = []
    for key in sorted(groups):
        group = groups[key]
        group["net_expense"] = group["expense"] - group["refund"]
        group["net_cashflow"] = group["income"] + group["refund"] - group["expense"]
        result.append({k: format(v, ".2f") if isinstance(v, Decimal) else v for k, v in group.items()})
    return result


# Version upgrades add entries. Keep existing implementations for historical reads.
NORMALIZERS = {LEGACY_NORMALIZER_VERSION: normalize_v1}
SUMMARIZERS = {LEGACY_SUMMARY_VERSION: summarize_v1}


def _implementation(registry, version, kind):
    if not isinstance(version, str) or version not in registry:
        raise DataError(f'unsupported_{kind}_version: 不支持此加工版本')
    return registry[version]


def normalize_record(row, ledger, utc_offset, version=None):
    selected = NORMALIZER_VERSION if version is None else version
    return _implementation(NORMALIZERS, selected, 'normalizer')(row, ledger, utc_offset)


def summarize(rows, version=None):
    selected = SUMMARY_VERSION if version is None else version
    return _implementation(SUMMARIZERS, selected, 'summary')(rows)


def snapshot_versions(snapshot):
    """Missing metadata denotes v1, never whichever version is current today."""
    if 'processing' not in snapshot:
        return LEGACY_NORMALIZER_VERSION, LEGACY_SUMMARY_VERSION
    metadata = snapshot['processing']
    if not isinstance(metadata, dict):
        raise DataError('invalid_processing: 加工版本信息无效')
    normalizer, summary = metadata.get('normalizer_version'), metadata.get('summary_version')
    _implementation(NORMALIZERS, normalizer, 'normalizer')
    _implementation(SUMMARIZERS, summary, 'summary')
    return normalizer, summary
