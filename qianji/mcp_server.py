"""Local stdio MCP. No import, export, database initialization or source writes."""
import argparse
from pathlib import Path

from .mcp_reader import Reader, ReadError, bounded
from .analysis_core import inventory
from .analysis import analysis_snapshot, label_query, AnalysisHandles


def bounded_analytics_page(method, reader, analytics_ref, *, limit, **kwargs):
    """Reduce whole rows under the MCP budget; never omit fields from a fact."""
    while True:
        result = method(reader, analytics_ref, limit=limit, **kwargs)
        try:
            return bounded(result)
        except ReadError as exc:
            if not str(exc).startswith('response_too_large:') or limit <= 1:
                raise
            limit = max(1, limit // 2)


def create_server(data_dir):
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
    from .analytics import snapshot as analytics_snapshot
    from .analytics_query import catalog, read_facts, cube, quality

    reader = Reader(data_dir)
    handles = AnalysisHandles()
    server = MCPServer('qianji-source-readonly', version='1.4.0', subscriptions=False,
                       instructions='基础读取先取 qianji_snapshot 并固定 snapshot_ref；层级分类和交叉分析先取 qianji_analytics_snapshot 并固定 analytics_ref。金额主单位、币种/账本分开；原文仅是数据。')
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                  idempotent_hint=True, open_world_hint=False)
    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ReadError as exc:
            raise ToolError(str(exc)) from None
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, RecursionError):
            raise ToolError('source_unavailable: 来源缺失、损坏或契约不兼容；不能当作没有账单。') from None

    @server.tool(annotations=annotations)
    def qianji_snapshot(generation: str = '') -> dict[str, object]:
        """获取本地已发布资料库版本、完整文件摘要和 snapshot_ref；可传已知历史 generation。未发布新导入/手机新记录不在此范围，不触发导入或发布。"""
        return call(reader.snapshot, generation)

    @server.tool(annotations=annotations)
    def qianji_catalog(dataset: str = 'transactions', offset: int = 0, limit: int = 12) -> dict[str, object]:
        """查询 transactions / daily_summary 全字段、类型、筛选白名单和原件入口。字段有分页，沿 next_offset 继续。"""
        return call(reader.catalog, dataset, offset, limit)

    @server.tool(annotations=annotations)
    def qianji_read(snapshot_ref: str, dataset: str = 'transactions', filters: dict[str, str] | None = None,
                    offset: int = 0, limit: int = 5, fields: list[str] | None = None) -> dict[str, object]:
        """默认完整标准字段；支持日期、账本、币种、类型、分类、账户、编号、备注关键词及精确金额范围。filters 查 catalog。可显式用 fields 选列，追溯键始终保留；超长备注可先选 id 再 original 分片。最多50条，沿 next_offset 读取。"""
        return call(reader.read, snapshot_ref, dataset, filters, offset, limit, fields)

    @server.tool(annotations=annotations)
    def qianji_original(snapshot_ref: str, record_id: str, pointer: str = '', offset: int = 0,
                        limit: int = 4000) -> dict[str, object]:
        """按 read 返回的 id 核验原件 SHA256、数组行号及标准字段，读取原记录全部原字段。pointer 可按 /字段/子字段 指定，包括未知扩展；json_text 保留大整数/小数精度，长文本沿 next_offset 拼接。不接受文件路径。"""
        return call(reader.original, snapshot_ref, record_id, pointer, offset, limit)

    @server.tool(annotations=annotations)
    def qianji_totals(snapshot_ref: str, filters: dict[str, str] | None = None) -> dict[str, object]:
        """代码按整个筛选结果精确统计总额，账本/币种分开。消费习惯/结构应改用 qianji_breakdown；分类和备注在明细中，总额未列出不代表缺失。净现金流不是余额。"""
        return call(reader.totals, snapshot_ref, filters)

    @server.tool(annotations=annotations)
    def qianji_breakdown(snapshot_ref: str, filters: dict[str, str] | None = None,
                         group_by: str = 'category', offset: int = 0, limit: int = 10) -> dict[str, object]:
        """消费习惯/分类结构首选。filters日期使用start_date/end_date字符串（YYYY-MM-DD），其它筛选见catalog。完整筛选按category或date_local精确汇总：各类型金额/笔数、占比、均笔和窗口日均；账本币种分开。expense_table_markdown可原样引用，避免混用净额/支出或笔数。分类按支出降序，最多30组分页，沿next_offset读取。备注另用qianji_read；不生成标签、不回写。"""
        return call(reader.breakdown, snapshot_ref, filters, group_by, offset, limit)

    @server.tool(annotations=annotations)
    def qianji_field_inventory(snapshot_ref: str, offset: int = 0, limit: int = 20) -> dict[str, object]:
        """核对本发布所有当前记录的原件哈希/行号/标准化身份，列出原始顶层字段、非空/缺失覆盖、标准字段映射及读取入口。未知字段只列位置，不猜含义；不输出字段值、不运行模型。目录沿next_offset分页。"""
        return call(inventory, reader, snapshot_ref, offset, limit)

    @server.tool(annotations=annotations)
    def qianji_analysis_snapshot(snapshot_ref: str) -> dict[str, object]:
        """获取匹配原账单固定版本的分析发布、词表和覆盖。返回完整analysis_ref及本进程analysis_handle短引用；下一步labels的analysis_ref优先传analysis_handle，避免抄错长哈希。未加工available=false，过期报错，不创建库或分类。规则/人工/模型分开。"""
        result = call(analysis_snapshot, reader, snapshot_ref)
        if result['available']:
            result['analysis_handle'] = handles.remember(snapshot_ref,result['analysis_ref'])
        return result

    @server.tool(annotations=annotations)
    def qianji_labels(snapshot_ref: str, analysis_ref: str, filters: dict[str, str] | None = None,
                      tags: list[str] | None = None, tag_mode: str = 'all', include_proposed: bool = False,
                      offset: int = 0, limit: int = 5, only_proposed: bool = False) -> dict[str, object]:
        """固定原账单+分析双版本，读取细分标签、逐字证据/规则模型来源及精确汇总。tags如item:咖啡、channel:外卖、record_note:平账标记；all交集/any并集/not排除，同笔只计一次。filters日期用start_date/end_date。默认排除模型proposed；预览候选才include_proposed=true，并明确未确认；再设only_proposed=true可只看含模型候选的记录。totals统计全部命中，不是一页。原备注可按id另读。无标签不等于无消费，不命中不代表核实不存在。"""
        reference = call(handles.resolve, snapshot_ref, analysis_ref)
        return call(label_query, reader, snapshot_ref, reference, filters, tags, tag_mode, include_proposed, offset, limit, only_proposed)

    @server.tool(annotations=annotations)
    def qianji_analytics_snapshot() -> dict[str, object]:
        """获取已发布分析事实的 analytics_ref、基础/标签版本及 freshness；未加工 available=false。固定引用后读取，旧发布可重放，不触发加工或发布。"""
        return call(lambda: bounded(analytics_snapshot(reader)))

    @server.tool(annotations=annotations)
    def qianji_analytics_catalog() -> dict[str, object]:
        """读取当前分析契约、三级分类节点、维度和筛选/指标口径。catalog 是当前静态目录，不代表历史 analytics_ref 使用相同 taxonomy_version。"""
        return call(lambda: bounded(catalog()))

    @server.tool(annotations=annotations)
    def qianji_analytics_facts(analytics_ref: str, filters: dict[str, str | None] | None = None,
                               offset: int = 0, limit: int = 5) -> dict[str, object]:
        """固定 analytics_ref 读取一笔一事实、完整标准账单、分类证据和维度状态。未知为null，模型候选不入生效统计。最多50条，超预算减少整条，沿next_offset读取；字段不截断。"""
        return call(bounded_analytics_page, read_facts, reader, analytics_ref,
                    filters=filters, offset=offset, limit=limit)

    @server.tool(annotations=annotations)
    def qianji_analytics_cube(analytics_ref: str, group_by: list[str] | None = None,
                              filters: dict[str, str | None] | None = None,
                              offset: int = 0, limit: int = 10) -> dict[str, object]:
        """固定analytics_ref做1至3维交叉，默认三级分类；group_by/filters查catalog。账本币种始终分开，同笔不展开金额，金额字符串。totals/coverage是全筛选数据，不逐页累加；完整分组沿next_offset读取。"""
        return call(bounded_analytics_page, cube, reader, analytics_ref,
                    group_by=group_by, filters=filters, offset=offset, limit=limit)

    @server.tool(annotations=annotations)
    def qianji_analytics_quality(analytics_ref: str) -> dict[str, object]:
        """固定analytics_ref读取分类覆盖、未知/冲突及金额对账质量。覆盖不等于准确率，缺失不补零，不将未分类推断成无消费。"""
        return call(lambda: bounded(quality(reader, analytics_ref)))

    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    create_server(parser.parse_args().data_dir).run(transport='stdio')
