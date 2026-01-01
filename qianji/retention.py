"""Reclaim derived analysis publications, preserving their current dependency graph."""
import hashlib
import json
from pathlib import Path

from ._retention import checked, prune


def prune_publications(root, *, grace_seconds=3600, apply=True):
    # Caller holds publication_lock. Source publications remain audit evidence
    # for saved evaluations in labels.sqlite3 and are not disposable cache here.
    root = Path(root).resolve()
    keep = {"analysis": set(), "analytics": set()}
    for folder, field in (("analysis", "analysis_ref"), ("analytics", "analytics_ref")):
        pointer = checked(root, root / folder / "exports/current.json")
        if pointer.exists():
            ref = json.loads(pointer.read_bytes())[field]
            fixed = checked(root, pointer.parent / "versions" / (ref + ".json"))
            raw = fixed.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref:
                raise ValueError("Current publication checksum mismatch")
            keep[folder].add(ref + ".json")
            if folder == "analytics":
                doc = json.loads(raw)
                for key in ("analysis_ref", "observed_analysis_ref"):
                    if doc.get(key):
                        keep["analysis"].add(doc[key] + ".json")
    result = {"removed": [], "bytes_removed": 0, "deferred": []}
    # Validate all pinned label files before removing any other publication.
    for name in keep["analysis"]:
        raw = checked(root, root / "analysis/exports/versions" / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() + ".json" != name:
            raise ValueError("Pinned labels checksum mismatch")
    for folder in keep:
        item = prune(root, root / folder / "exports/versions", keep[folder],
                     r"[a-f0-9]{64}\.json", grace_seconds=grace_seconds, apply=apply)
        result["removed"].extend(item["removed"])
        result["bytes_removed"] += item["bytes_removed"]
        result["deferred"].extend(item["deferred"])
    return result
