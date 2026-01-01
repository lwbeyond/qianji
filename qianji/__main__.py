"""Run from the project directory: python -m qianji --help."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .api import Server
from .normalize import DataError
from .store import Store

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data"


def main(argv=None):
    parser = argparse.ArgumentParser(description="钱迹本地导入、去重与只读数据接口")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA, help="私密资料目录（默认项目下 data）")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("preview", "import"):
        sub = commands.add_parser(command, help="预览差异" if command == "preview" else "归档并导入")
        sub.add_argument("file", type=Path)
        sub.add_argument("--ledger", default="main", help="稳定的账本标识；不同账本必须使用不同标识")
        sub.add_argument("--utc-offset", default="+08:00")
        if command == "import":
            sub.add_argument("--accept-updates", action="store_true")
            sub.add_argument("--expected-generation", help="接纳修订必须填写预览中的 generation_before")
            sub.add_argument("--no-analysis", action="store_true", help="本次仅导入发布，不启动后台增量标签处理")
    commands.add_parser("status", help="查看数量和覆盖日期")
    commands.add_parser("verify", help="检查数据库、修订历史和原始归档")
    rebuild = commands.add_parser("rebuild", help="从已接纳原件重建基础数据；默认只预览")
    rebuild.add_argument("--apply", action="store_true", help="校验后应用重建并发布")
    rebuild.add_argument("--expected-plan", help="应用时必须提供最新预览的 plan_ref")
    rebuild.add_argument("--no-analysis", action="store_true", help="仅重建基础数据，不启动可选标签加工")
    reprocess = commands.add_parser("reprocess", help="重处理三级分类和分析事实；默认预览，不修改基础账单")
    reprocess.add_argument("--apply", action="store_true")
    reprocess.add_argument("--expected-plan", help="应用时提供本次预览的 plan_ref")
    export = commands.add_parser("export", help="原子写入标准化 JSON 快照")
    export.add_argument("--output", type=Path)
    export.add_argument("--no-analysis", action="store_true", help="本次仅发布，不启动后台增量标签处理")
    history = commands.add_parser("history", help="查看单条记录的所有已接受版本")
    history.add_argument("id")
    serve = commands.add_parser("serve", help="启动本机只读接口")
    serve.add_argument("--port", type=int, default=8790)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        if args.command == 'reprocess':
            from .analytics import reprocess
            result = reprocess(args.data_dir, apply=args.apply, expected_plan=args.expected_plan)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        store = Store(args.data_dir)
        if args.command == 'rebuild':
            result = store.rebuild(apply=args.apply, expected_plan=args.expected_plan)
            if result['state'] == 'completed' and not args.no_analysis:
                result['analysis'] = schedule_analysis(args.data_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command in ("preview", "import"):
            result = store.import_file(args.file, ledger=args.ledger, utc_offset=args.utc_offset,
                                       preview=args.command == "preview",
                                       accept_updates=getattr(args, "accept_updates", False),
                                       expected_generation=getattr(args, "expected_generation", None))
            if result["applied"]:
                try:
                    result["exported_snapshot"] = store.export()
                except (OSError, sqlite3.Error, DataError) as exc:
                    raise DataError("导入已保存，但快照发布失败；请运行 python -m qianji export 重试。") from exc
                if not args.no_analysis:
                    result['analysis'] = schedule_analysis(args.data_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if args.command == "import" and not result["applied"] else 0
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise DataError("端口必须为 1–65535。")
            with Server(store, args.port) as server:
                print(f"钱迹只读接口：http://127.0.0.1:{server.server_port}（Ctrl+C 停止）", flush=True)
                server.serve_forever()
            return 0
        result = {"status": store.status, "verify": store.verify,
                  "export": lambda: store.export(args.output),
                  "history": lambda: store.history(args.id)}[args.command]()
        if args.command == 'export' and not args.output and not args.no_analysis:
            result['analysis'] = schedule_analysis(args.data_dir)
        if args.command == 'status':
            from .auto_analysis import status
            result['analysis'] = status(args.data_dir)
            from .analytics import snapshot
            from .mcp_reader import Reader
            result['analytics'] = snapshot(Reader(args.data_dir))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DataError, ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


def schedule_analysis(data_dir):
    """A derived-analysis problem cannot turn a committed import into a failed import."""
    from .auto_analysis import enqueue
    from .mcp_reader import ReadError
    try:
        return enqueue(data_dir)
    except (ReadError, OSError, ValueError, sqlite3.Error) as error:
        code = str(error).split(':', 1)[0] if isinstance(error, ReadError) else type(error).__name__
        return {'state': 'needs_attention', 'error_code': code,
                'message': '账单已导入发布，自动分类排队失败；修复后运行 python -m qianji.auto_analysis enqueue'}


if __name__ == "__main__":
    raise SystemExit(main())
