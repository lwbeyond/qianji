"""Transactional current records, immutable revisions and original-file archives."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from .normalize import DataError, canonical, digest, load_export
from . import processing

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledgers (id TEXT PRIMARY KEY, utc_offset TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY, ledger_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
  source_name TEXT NOT NULL, imported_at TEXT NOT NULL, report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY, ledger_id TEXT NOT NULL, source_key TEXT NOT NULL,
  revision INTEGER NOT NULL, raw_json TEXT NOT NULL, normalized_json TEXT NOT NULL,
  UNIQUE(ledger_id, source_key)
);
CREATE TABLE IF NOT EXISTS revisions (
  record_id TEXT NOT NULL, revision INTEGER NOT NULL, import_id INTEGER NOT NULL,
  raw_json TEXT NOT NULL, normalized_json TEXT NOT NULL,
  PRIMARY KEY(record_id, revision), FOREIGN KEY(import_id) REFERENCES imports(id)
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def immutable_write(path, content):
    """Publish a fully written generation file once, refusing semantic drift."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise DataError("同一 generation 已有不同内容，拒绝替换固定发布；请核对来源与契约。")
    finally:
        os.unlink(name)


def daily_summary(rows):
    return processing.summarize(rows)


class Store:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "qianji.sqlite3"
        with closing(self.connect()) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise DataError("不支持此资料库版本。")
            db.executescript(SCHEMA)
            for key, value in (("library_id", str(uuid4())), ("revision", "0"), ("updated_at", now())):
                db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (key, value))
            # Migration adds metadata only. Existing publications and source history
            # remain byte-for-byte unchanged until an explicit rebuild is applied.
            meta = self._metadata(db)
            for key, value in (("source_revision", meta["revision"]),
                               ("source_updated_at", meta["updated_at"])):
                db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (key, value))
            if version == 0:
                for key, value in (("normalizer_version", processing.NORMALIZER_VERSION),
                                   ("summary_version", processing.SUMMARY_VERSION),
                                   ("processed_at", meta["updated_at"])):
                    db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (key, value))
            db.execute("PRAGMA user_version=2")

    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _metadata(db):
        return dict(db.execute("SELECT key, value FROM metadata").fetchall())

    @staticmethod
    def _generation(meta):
        return meta["library_id"] + ":" + meta["revision"]

    def import_file(self, path, *, ledger="main", utc_offset="+08:00", preview=False,
                    accept_updates=False, expected_generation=None):
        content, incoming, duplicates = load_export(path, ledger, utc_offset)
        source_sha = hashlib.sha256(content).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            meta = self._metadata(db)
            if meta.get("pending_rebuild"):
                raise DataError("publication_pending: 请先运行 python -m qianji export 完成重建发布。")
            if (meta.get("normalizer_version", "qianji-normalize/1") != processing.NORMALIZER_VERSION
                    or meta.get("summary_version", "qianji-summary/1") != processing.SUMMARY_VERSION):
                raise DataError("processing_upgrade_required: 加工版本已变化，请先预览并应用 rebuild，再导入新数据。")
            generation = self._generation(meta)
            if expected_generation is not None and expected_generation != generation:
                raise DataError("资料库已变化，请重新预览后导入。")
            config = db.execute("SELECT utc_offset FROM ledgers WHERE id=?", (ledger,)).fetchone()
            if config and config[0] != utc_offset:
                raise DataError("同一账本的时区偏移不能在导入时改变。")
            report = {"mode": "preview" if preview else "import", "generation_before": generation,
                      "source_sha256": source_sha, "ledger_id": ledger, "unique_records": len(incoming),
                      "duplicates_in_file": duplicates, "added": 0, "unchanged": 0, "updated": 0,
                      "conflicts": [], "applied": False}
            pending = []
            for normalized, raw, index in incoming:
                old = db.execute("SELECT * FROM records WHERE id=?", (normalized["id"],)).fetchone()
                if old and old["raw_json"] == raw:
                    report["unchanged"] += 1
                    continue
                revision = old["revision"] + 1 if old else 1
                if old:
                    report["updated"] += 1
                    before, after = json.loads(old["raw_json"]), json.loads(raw)
                    changed = sorted(k for k in before.keys() | after.keys()
                                     if (k in before) != (k in after) or before.get(k) != after.get(k))
                    report["conflicts"].append({"id": normalized["id"], "current_revision": old["revision"],
                                                "changed_fields": changed})
                else:
                    report["added"] += 1
                normalized.update(revision=revision, source_file_sha256=source_sha, source_row=index)
                pending.append((normalized, raw))
            report["generation_after"] = generation
            if preview or (report["conflicts"] and not accept_updates):
                return report
            if report["conflicts"] and expected_generation is None:
                raise DataError("接纳修订时必须提供预览返回的 --expected-generation。")
            archive = self.root / "archive" / (source_sha + ".json")
            if archive.exists():
                if hashlib.sha256(archive.read_bytes()).hexdigest() != source_sha:
                    raise DataError("原始归档校验失败，停止导入。")
            else:
                # Archive first: a failed DB transaction may leave an unreferenced source,
                # but a successful transaction can never reference an unwritten archive.
                atomic_write(archive, content)
            db.execute("INSERT OR IGNORE INTO ledgers VALUES (?, ?)", (ledger, utc_offset))
            imported_at = now()
            cursor = db.execute("INSERT INTO imports (ledger_id,source_sha256,source_name,imported_at,report_json) VALUES (?,?,?,?,?)",
                                (ledger, source_sha, Path(path).name, imported_at, "{}"))
            for normalized, raw in pending:
                doc = canonical(normalized)
                db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)",
                           (normalized["id"], ledger, normalized["source_key"], normalized["revision"], raw, doc))
                db.execute("INSERT INTO revisions VALUES (?,?,?,?,?)",
                           (normalized["id"], normalized["revision"], cursor.lastrowid, raw, doc))
            if pending or not config:
                meta["revision"] = str(int(meta["revision"]) + 1)
                db.execute("UPDATE metadata SET value=? WHERE key='revision'", (meta["revision"],))
                db.execute("UPDATE metadata SET value=? WHERE key='updated_at'", (imported_at,))
                db.execute("UPDATE metadata SET value=? WHERE key='source_revision'",
                           (str(int(meta["source_revision"]) + 1),))
                db.execute("UPDATE metadata SET value=? WHERE key='source_updated_at'", (imported_at,))
                if meta.get("normalizer_version"):
                    db.execute("UPDATE metadata SET value=? WHERE key='processed_at'", (imported_at,))
            report.update(applied=True, generation_after=self._generation(meta))
            db.execute("UPDATE imports SET report_json=? WHERE id=?", (canonical(report), cursor.lastrowid))
            return report

    def snapshot(self):
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            meta = self._metadata(db)
            if meta.get("pending_rebuild"):
                from integrations.qianji_client import load_snapshot
                # A committed candidate is not public until current.json is replaced.
                return load_snapshot(self.root / "exports" / "current.json",
                                     expected_library_id=meta["library_id"])
            return self._snapshot(db, meta)

    def _snapshot(self, db, meta=None):
        meta = meta or self._metadata(db)
        rows = [json.loads(row[0]) for row in db.execute("SELECT normalized_json FROM records")]
        rows.sort(key=lambda row: (row["date_local"], row["occurred_at"], row["id"]))
        ledgers = [dict(row) for row in db.execute("SELECT id, utc_offset FROM ledgers ORDER BY id")]
        datasets = {"transactions": rows, "daily_summary": processing.summarize(
            rows, meta.get("summary_version", "qianji-summary/1"))}
        doc = {"format": "qianji-normalized", "format_version": 1,
                "snapshot": {"generation": self._generation(meta), "library_id": meta["library_id"],
                             "revision": int(meta["revision"]), "updated_at": meta["updated_at"],
                             "contract_version": "1.0", "ledgers": ledgers,
                             "available_datasets": list(datasets),
                             "data_sha256": digest(canonical(datasets)),
                             "counts": {k: len(v) for k, v in datasets.items()}},
                "datasets": datasets}
        if meta.get("normalizer_version"):
            doc["snapshot"].update(contract_version="1.1", processing={
                "normalizer_version": meta["normalizer_version"], "summary_version": meta["summary_version"],
                "source_revision": int(meta["source_revision"]), "source_updated_at": meta["source_updated_at"],
                "processed_at": meta["processed_at"]})
        return doc

    def export(self, output=None):
        target = Path(output) if output else self.root / "exports" / "current.json"
        # Serialize publication with imports so concurrent exporters cannot replace a
        # newer current.json with an older generation.
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            meta = self._metadata(db)
            # Keep the normal snapshot path for established callers; pending rebuilds
            # alone need the internal candidate rather than the public old snapshot.
            doc = self._snapshot(db, meta) if meta.get("pending_rebuild") else self.snapshot()
            from integrations.qianji_client import QianjiAPIError, validate_snapshot
            try:
                validate_snapshot(doc, expected_library_id=meta["library_id"])
            except QianjiAPIError as exc:
                raise DataError('snapshot_validation: ' + str(exc)) from exc
            payload = (canonical(doc) + "\n").encode("utf-8")
            name = hashlib.sha256(doc["snapshot"]["generation"].encode("utf-8")).hexdigest() + ".json"
            fixed = target.parent / "versions" / name
            immutable_write(fixed, payload)
            atomic_write(target, payload)
            result = {**doc["snapshot"], "publication": {
                "path": str(fixed.resolve()), "sha256": hashlib.sha256(payload).hexdigest(),
                "retention": "keep_all_no_automatic_deletion"}}
            pending = meta.get("pending_rebuild") if output is None else None
            if pending:
                from .rebuild import finish_publication
                finish_publication(self, pending, result)
                db.execute("DELETE FROM metadata WHERE key='pending_rebuild'")
        if output is None:
            from .analytics import refresh
            result['analytics'] = refresh(self.root)
        return result

    def rebuild(self, *, apply=False, expected_plan=None):
        from .rebuild import rebuild
        return rebuild(self, apply=apply, expected_plan=expected_plan)

    def history(self, record):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT normalized_json FROM revisions WHERE record_id=? ORDER BY revision", (record,))]

    def status(self):
        doc = self.snapshot()
        rows = doc["datasets"]["transactions"]
        with closing(self.connect()) as db:
            imports = db.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
            revisions = db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
            meta = self._metadata(db)
        last = self.root / "processing" / "last-run.json"
        if meta.get("pending_rebuild"):
            last = self.root / 'processing' / 'runs' / (meta['pending_rebuild'] + '.json')
        rebuild_status = json.loads(last.read_bytes()) if last.exists() else {"state": "not_started"}
        if meta.get('pending_rebuild'):
            rebuild_status.update(state='publication_pending', applied=True)
        elif rebuild_status['state'] == 'candidate_prepared':
            rebuild_status.update(state='interrupted', applied=False)
        return {**doc["snapshot"], "import_runs": imports, "record_revisions": revisions,
                "rebuild": rebuild_status,
                "publication_pending": bool(meta.get("pending_rebuild")),
                "date_range": [rows[0]["date_local"], rows[-1]["date_local"]] if rows else [],
                "kinds": {kind: sum(r["kind"] == kind for r in rows)
                          for kind in ("expense", "income", "refund", "transfer")}}

    def verify(self):
        with closing(self.connect()) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
            sources = [r[0] for r in db.execute("SELECT DISTINCT source_sha256 FROM imports")]
            bad_current = db.execute("""SELECT COUNT(*) FROM records c LEFT JOIN revisions r
                ON c.id=r.record_id AND c.revision=r.revision
                WHERE r.record_id IS NULL OR c.raw_json!=r.raw_json""").fetchone()[0]
        if integrity != "ok" or foreign_keys or bad_current:
            raise DataError("资料库或修订历史校验失败。")
        for sha in sources:
            path = self.root / "archive" / (sha + ".json")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise DataError("原始文件归档缺失或内容已改变。")
        from .rebuild import accepted_rows
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            meta = self._metadata(db)
            current, rebuilt = accepted_rows(self, db, meta.get("normalizer_version", "qianji-normalize/1"))
            if current != rebuilt:
                raise DataError("normalization_mismatch: 当前标准化记录与已接纳原件及加工版本不一致。")
        return {"ok": True, "archives_checked": len(sources), "database": "ok", "revisions": "ok"}
