"""Loopback-only, read-only HTTP adapter for local consumer applications."""

from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from urllib.parse import parse_qs, urlsplit

from .normalize import canonical, _pairs, _bad_number
from .mcp_reader import Reader, ReadError


def read_analytics(reader, name, params):
    """The analytical projection is file-backed and never falls back to live rows."""
    from .analytics import snapshot
    from .analytics_query import catalog, read_facts, cube, quality

    allowed = {
        'snapshot': set(), 'catalog': set(), 'quality': {'analytics_ref'},
        'facts': {'analytics_ref', 'filters', 'offset', 'limit'},
        'cube': {'analytics_ref', 'filters', 'group_by', 'offset', 'limit'},
    }
    if name not in allowed:
        raise APIError('not_found', 'Unknown analytics endpoint', 404)
    if params.keys() - allowed[name]:
        raise APIError('invalid_query', 'Unknown analytics query parameter')
    if name == 'snapshot':
        return snapshot(reader)
    if name == 'catalog':
        return catalog()
    ref = params.get('analytics_ref')
    if not ref:
        raise APIError('analytics_ref_required', 'Read analytics/snapshot first and pin analytics_ref')
    if name == 'quality':
        return quality(reader, ref)
    filters = None
    if 'filters' in params:
        try:
            filters = json.loads(params['filters'], object_pairs_hook=_pairs, parse_constant=_bad_number)
        except (ValueError, RecursionError) as exc:
            raise APIError('invalid_filter', 'filters must be a strict JSON object') from exc
        if not isinstance(filters, dict):
            raise APIError('invalid_filter', 'filters must be a JSON object')
    pagination = {}
    for key, default in (('offset', '0'), ('limit', '20' if name == 'facts' else '30')):
        value = params.get(key, default)
        if not re.fullmatch(r'[0-9]+', value):
            raise APIError('invalid_pagination', 'offset and limit must be unsigned integers')
        try:
            pagination[key] = int(value)
        except ValueError as exc:
            raise APIError('invalid_pagination', 'Pagination integer is too large') from exc
    if name == 'facts':
        return read_facts(reader, ref, filters=filters, **pagination)
    group_by = params['group_by'].split(',') if 'group_by' in params else None
    return cube(reader, ref, group_by=group_by, filters=filters, **pagination)


def analytics_error(exc):
    code = str(exc).split(':', 1)[0]
    if code.startswith(('invalid_', 'unsupported_')) or code.endswith('_required'):
        status = 400
    elif code.endswith(('_changed', '_stale', '_conflict')):
        status = 409
    else:
        status = 503
    return APIError(code, str(exc), status)


class APIError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def read_dataset(doc, name, params):
    if name not in doc["datasets"]:
        raise APIError("not_found", "Unknown dataset", 404)
    allowed = {"generation", "limit", "offset", "start_date", "end_date", "ledger_id", "currency"}
    if name == "transactions":
        allowed |= {"kind", "category"}
    if params.keys() - allowed:
        raise APIError("invalid_filter", "Unknown query parameter")
    if not params.get("generation"):
        raise APIError("generation_required", "Read /api/v1/snapshot first and pin its generation")
    if params["generation"] != doc["snapshot"]["generation"]:
        raise APIError("snapshot_changed", "Data changed; restart the entire read with a new snapshot", 409)
    try:
        limit, offset = int(params.get("limit", "500")), int(params.get("offset", "0"))
    except ValueError as exc:
        raise APIError("invalid_pagination", "limit and offset must be integers") from exc
    if not 1 <= limit <= 5000 or offset < 0:
        raise APIError("invalid_pagination", "limit must be 1..5000 and offset must be nonnegative")
    for field in ("start_date", "end_date"):
        if field in params:
            try:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", params[field]):
                    raise ValueError()
                date.fromisoformat(params[field])
            except ValueError as exc:
                raise APIError("invalid_date", "Use YYYY-MM-DD") from exc
    if params.get("start_date", "") > params.get("end_date", "9999-12-31"):
        raise APIError("invalid_date_range", "start_date must not exceed end_date")
    if "kind" in params and params["kind"] not in {"expense", "income", "refund", "transfer"}:
        raise APIError("invalid_kind", "Unknown transaction kind")
    if "currency" in params and not re.fullmatch(r"[A-Z]{3}", params["currency"]):
        raise APIError("invalid_currency", "Use a three-letter uppercase currency code")
    rows = [r for r in doc["datasets"][name]
            if params.get("start_date", "") <= r["date_local"] <= params.get("end_date", "9999-12-31")
            and all(r[k] == params[k] for k in ("ledger_id", "currency", "kind", "category") if k in params)]
    page = rows[offset:offset + limit]
    return {"api_version": "1.0", "snapshot": doc["snapshot"], "dataset": name, "data": page,
            "pagination": {"limit": limit, "offset": offset, "returned": len(page), "total": len(rows),
                           "next_offset": offset + len(page) if offset + len(page) < len(rows) else None}}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store, port=8790):
        self.store = store
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Do not put query filters or personal values into access logs.

    def _json(self, status, payload):
        body = canonical(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                raise APIError("forbidden_host", "Only local hosts are allowed", 403)
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {"http://" + host for host in hosts}:
                raise APIError("forbidden_origin", "Use a local backend client", 403)
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise APIError("forbidden_origin", "Cross-site browser requests are not allowed", 403)
            request = urlsplit(self.path)
            query = parse_qs(request.query, keep_blank_values=True, max_num_fields=20)
            if any(len(values) != 1 for values in query.values()):
                raise APIError("invalid_query", "Duplicate query parameters")
            params = {key: values[0] for key, values in query.items()}
            if request.path == "/health":
                if params:
                    raise APIError("invalid_query", "Health does not accept parameters")
                self._json(200, {"ok": True, "app": "qianji-local", "api_version": "1.0"})
                return
            if request.path.startswith('/api/v1/analytics/'):
                try:
                    result = read_analytics(Reader(self.server.store.root),
                                            request.path.removeprefix('/api/v1/analytics/'), params)
                except APIError:
                    raise
                except ReadError as exc:
                    raise analytics_error(exc) from exc
                except (OSError, ValueError, RecursionError) as exc:
                    raise APIError('source_unavailable', 'Analytical publication unavailable', 503) from exc
                self._json(200, result)
                return
            doc = self.server.store.snapshot()
            contract = self.headers.get('X-Qianji-Contract-Version', '1.0')
            if contract not in ('1.0', '1.1'):
                raise APIError('unsupported_contract', 'Supported data contracts: 1.0, 1.1')
            if contract == '1.0' and doc['snapshot']['contract_version'] == '1.1':
                # Keep the original HTTP contract available for existing consumers.
                # The datasets and generation are identical across both views.
                metadata = dict(doc['snapshot'])
                metadata.pop('processing')
                metadata.update(contract_version='1.0')
                doc = {**doc, 'snapshot': metadata}
            if request.path == "/api/v1/snapshot":
                if params:
                    raise APIError("invalid_query", "Snapshot does not accept parameters")
                result = {"api_version": "1.0", "snapshot": doc["snapshot"]}
            elif request.path.startswith("/api/v1/datasets/"):
                result = read_dataset(doc, request.path.removeprefix("/api/v1/datasets/"), params)
            else:
                raise APIError("not_found", "Unknown endpoint", 404)
            self._json(200, result)
        except APIError as exc:
            self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except ValueError:
            self._json(400, {"error": {"code": "invalid_query", "message": "Invalid query string"}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._json(500, {"error": {"code": "internal_error", "message": "Cannot read local data"}})

    def do_POST(self):
        self._json(405, {"error": {"code": "read_only", "message": "Import through the local CLI"}})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
