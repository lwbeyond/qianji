"""Strict export parsing with the current versioned normalization entrypoint."""

from decimal import Decimal
import json
from pathlib import Path
import re

# Existing imports remain compatible; version 1 owns its complete helper semantics.
from .normalization_v1 import (DataError, MAX_BYTES, KINDS, canonical, digest,
                               record_id, _pairs, _bad_number, json_safe,
                               parse_offset, money, _text, _asset)
from .processing import normalize_record


def normalize(row, ledger, utc_offset):
    return normalize_record(row, ledger, utc_offset)


def load_export(path, ledger="main", utc_offset="+08:00"):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", ledger):
        raise DataError("账本标识限 1–64 位英文字母、数字、下划线、点和横线。")
    parse_offset(utc_offset)
    with Path(path).open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise DataError("导出文件超过 128 MiB。")
    try:
        rows = json.loads(content.decode("utf-8-sig"), parse_float=Decimal,
                          parse_constant=_bad_number, object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DataError("文件不是有效的 UTF-8 JSON。") from exc
    if not isinstance(rows, list):
        raise DataError("当前支持钱迹 JSON 数组导出，不支持 CSV 或其他备份格式。")
    unique, duplicates = {}, 0
    for index, row in enumerate(rows, 1):
        try:
            normalized, raw = normalize(row, ledger, utc_offset)
        except DataError as exc:
            raise DataError(f"第 {index} 条：{exc}") from exc
        key = normalized["source_key"]
        if key in unique:
            if unique[key][1] != raw:
                raise DataError(f"第 {index} 条：同一文件中相同编号存在不同内容，拒绝猜测版本。")
            duplicates += 1
        else:
            unique[key] = (normalized, raw, index)
    return content, list(unique.values()), duplicates
