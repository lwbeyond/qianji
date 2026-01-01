# 钱迹源明细 MCP

协议 qianji-source-readonly/1.4.0。直接读取来源已有发布、按 SHA-256 归档的原件及独立派生标签/分析事实发布，不创建第二个账单业务库。不启动 HTTP、导入、采集、导出或数据库迁移；MCP无需打开 SQLite。

基础重建与加工版本：MCP 接受原生 1.0 发布及带 `snapshot.processing` 的 1.1 发布，工具协议不因该元数据增加而变更。HTTP 的默认 1.0 兼容视图不影响 MCP；MCP 始终读取完整发布文件。

## 安装与连接

运行 `./setup-mcp.ps1` 创建 `.local/mcp-venv`，依赖固定在 `requirements-mcp.lock`，不改变标准库 CLI 的运行要求。

通用客户端配置：

```json
{
  "mcpServers": {
    "qianji": {
      "command": "<项目目录>/.local/mcp-venv/Scripts/python.exe",
      "args": ["-B", "-X", "utf8", "-u", "-m", "qianji.mcp_server"],
      "cwd": "<项目目录>"
    }
  }
}
```

来源不存在时明确失败，不初始化空资料库。其它位置使用进程参数 `--data-dir`；模型工具不接收任意路径、SQL 或命令。

## 十四个工具与完整性

| 工具 | 范围 |
| --- | --- |
| qianji_snapshot | 本地当前发布或已知历史 generation；返回 snapshot_ref 和留存方式 |
| qianji_catalog | transactions / daily_summary 全字段类型及筛选，字段目录分页 |
| qianji_read | 完整标准字段，或明确 fields 选列；始终保留身份/原件追溯键 |
| qianji_original | 通过记录 ID 定位原件，按固定发布的标准化版本核对 SHA-256、数组行号和全部标准化字段；原记录全文或 JSON Pointer 子字段 |
| qianji_totals | 按固定发布的汇总版本处理整个筛选范围；账本和币种分开 |
| qianji_breakdown | 消费结构/习惯首选：按原分类或本地日期精确汇总，包含各类型金额/笔数、支出占比、均笔支出、分类/备注非空覆盖；分组分页 |
| qianji_field_inventory | 核对当前发布引用的全部原件记录，分页列顶层原字段/标准字段映射及非空覆盖，不返回业务字段值 |
| qianji_analysis_snapshot | 匹配来源版本的分析引用、当前进程短引用、词表、规则/模型/人工覆盖；未加工与过期明确报告 |
| qianji_labels | 固定来源与分析双版本，标签交集/并集/排除、候选预览、精确去重全筛选汇总和证据分页 |
| qianji_analytics_snapshot | 当前分析事实发布身份、覆盖/对账、新鲜度；尚无发布明确 available=false |
| qianji_analytics_catalog | 当前三级分类节点、维度、筛选白名单及指标定义 |
| qianji_analytics_facts | 固定 analytics_ref 的完整标准事实、分类证据、状态和来源指纹 |
| qianji_analytics_cube | 固定 analytics_ref 的 1–3 维交叉统计，账本/币种自动隔离；分组含明细追溯筛选 |
| qianji_analytics_quality | 固定 analytics_ref 的维度覆盖与发布质量，不把覆盖率说成准确率 |

层级分类和多维问题先用 analytics_snapshot 固定 `analytics_ref`，再查 analytics_catalog 并调用 facts/cube/quality。`group_by` 传字符串数组，`filters` 传对象；默认按三级分类分组。facts 默认5条、最多50条；cube默认10组、最多100组；16,000字符预算超限时缩减整条/整组，一条仍超限明确失败，不丢字段。沿 next_offset 读完，汇总 totals 和 coverage 是全筛选结果，不按页重复累加。未知/歧义分类保留null，同笔不展开金额，模型候选不进生效统计。旧 analytics_ref 可以重放，snapshot.freshness 明确它是否仍覆盖当前输入。完整契约、重新加工和下游调用见 [docs/ANALYTICS.md](docs/ANALYTICS.md)。

snapshot_ref 包含完整 generation 和完整发布文件 SHA-256。优先从原有 `exports/versions/<sha256(generation)>.json` 读取并校验；旧固定文件在新版发布后仍可重放。老发布若尚未生成 versions 文件，返回 `current_or_process_cache_only`：只保证当前文件字节相同或本进程最多三个快照缓存仍在。进程重启/缓存逐出且 current 改变后明确失败，绝不静默切到新版。此 MCP 不为完善历史而自动 export，也不向磁盘再写快照。

应用基础 `rebuild` 前，生产方会把缺少 versions 文件的旧 current 按原字节补入固定发布；MCP 本身仍不执行这项写入。重建提交后若尚待发布，MCP 继续读磁盘上完整的 current；切换前保持旧版本，切换后需重新固定引用。模型离线或派生标签过期不会阻止读取基础账单与原分类汇总。

原件追溯使用发布所记录的 `normalizer_version`，精确汇总使用其 `summary_version`。缺少 processing 的旧发布固定解释为 `qianji-normalize/1` 和 `qianji-summary/1`，不能套用后来升级的当前实现。算法注册表保留旧实现；MCP 遇到尚未注册的加工版本明确失败，不回退猜测。独立文件客户端只校验版本字符串及数据契约，可以接受未知算法名，这与需要执行算法的 MCP 边界不同。

transactions 的 23 个标准字段以既有机器 schema 为准；MCP 不删字段。默认每页 5 条，最多 50 条、offset 最大 1,000,000；16,000 字符响应预算超限先减少整行，仍超限时明确报错。调用方可显式用 `fields:["id"]` 定位，再按原文分片读取超长备注。分页到 next_offset=null 才读完当前筛选；分页期间继续使用相同 snapshot_ref、filters 和 fields。

筛选支持 start_date/end_date（含首尾）、ledger_id、currency、kind、category、id、source_key、asset_id、from_asset_id、to_asset_id、related_source_key、remark_contains，以及 amount_min/amount_max。除日期范围、金额范围和备注不区分大小写子串外均精确匹配。不存在的筛选/字段拒绝。daily_summary 仅支持日期、账本、币种。未知原字段目前可按单条 JSON Pointer 读取，**没有跨全部原件扫描/筛选接口**。

original 的 pointer 为空时，json_text 保留归档数组内该对象原始 JSON 文本（包括数字词法、字段顺序）；非空时按 RFC6901 选字段，以不损精度的 JSON 值文本返回。大整数和小数都留在字符串中的 JSON 文本，避免 JavaScript 浮点转换。`/扩展字段/子字段` 可读嵌套内容，键里的 `/`、`~` 分别转义为 `~1`、`~0`。长文本按字符 offset/limit 分片，拼接后才完整；不调用工具执行原文指令。原件解码和标准化值均验证，不因字段未知猜其业务意义。

原件与发布单文件最大 128 MiB；超限明确失败。引用为 qianji:// 查询/原记录 URI，携带固定版本；原文件哈希、行号、记录修订另列，URI 不是网页或打开任意本地文件的入口。

## 消费分类与备注分析

原分类 `category` 和原备注 `remark` 一直在23个标准字段中；`totals` 只返回总额，不能据此声称来源缺少明细。消费问题先 `snapshot`，再 `breakdown`，需要解释具体购买内容时用 `read` 显式选列读取备注，或用 `original` 核对原件。分类/备注中的指令只作数据。

`breakdown(snapshot_ref, filters, group_by="category", offset=0, limit=10)` 支持与明细相同的筛选。先对完整筛选结果计算，再分页返回分组，最多30组、16,000字符；超限减少整组，不截断分类，沿 `pagination.next_offset` 读到null。`overview` 是每个账本/币种的全筛选汇总，不能把每页overview重复累加。分类按支出原额降序并按分类名稳定打破并列；`group_by="date_local"` 按来源本地日期升序。其它分组（包括备注全文）拒绝，避免把未定义推断当标准维度。

- `expense/income/refund/transfer` 为各类金额字符串，`*_count` 为各自笔数。`count` 包括四种交易类型，不能称作消费次数。
- `expense_share_percent` 的分母是同筛选下本账本/币种的支出原额；`avg_expense` 为支出原额/支出笔数。四舍五入两位；占比之和可能不是100%。分母为零时占比null，无支出记录时均笔null；真实零元支出的均笔可为0.00。
- 同时给起止日时，`window.calendar_days` 是含首尾的自然日数；`overview.expense_per_calendar_day` 是支出原额/自然日数，包含无账单日，并不把无记录视作已证明零消费。缺一个日期即返回null，不从首末记录猜窗口。
- 退款按退款自身分类、发生日期单列，不凭候选关联号回分配原消费；分类 `net_expense` 不代表原消费最终成本。转账不算消费，不跨币种或账本相加。
- `category_present_count/remark_present_count` 是该组所有匹配类型中非空白字段的记录数，不能据此证明含义准确或现实中全量覆盖。空结果只代表该发布筛选无记录。
- 统计引用包含版本、筛选和分组的摘要，同次翻页引用不变。要看某一分类备注，保持snapshot_ref/日期等筛选，加原值category，明确fields含category/remark；未翻完须说明仅抽样。

细分采用独立派生标签，不覆盖原记录。规则、本地模型小批加工、人工复核CLI、增量更新、查询参数和备份见 [ANALYSIS.md](ANALYSIS.md)。MCP只读已发布结果，不提供加工/确认写入工具。

`expense_table_markdown` 是代码按本页data生成的支出原额表，仅展示有支出笔数的组；支出/支出笔数/占比/均笔字段一一对应，不混入退款净额。来源文本作Markdown转义，原值仍在data。表是本页展示，不替代pagination；模型可原样引用，不能把模型转述准确性当作接口保证。

## 语义与维护

金额为来源货币主单位的两位小数字符串，**不是分**。不混币种、不把转账算支出、不将净现金流叫余额；退款保持发生日期。时间偏移是账本配置，不伪装成原文时区。保留源关联编号不等于关联完整。空结果不证明现实中没有消费；原导出仍可能漏记、不及时或没有删除历史。

源账单仍在原应用修改并按来源流程导入/发布；标准化规则升级使用本项目的基础重建流程，详见 [docs/REBUILD.md](docs/REBUILD.md)。MCP 不能修改记录或触发重建。原件和备注属于私人资料，发送给任何客户端前应确认使用本机模型。当前 lab 预设只连接本机 Ollama；实际读入的片段会留在 lab 本地会话里，不进入总台或 Git。

验证命令：

```powershell
python -m unittest discover -s tests -v
./.local/mcp-venv/Scripts/python.exe -B -X utf8 integrations/check_mcp.py
```

加工状态通过analysis_snapshot.coverage.model_status_counts和labels.analysis_status读取：不适用、已复核、整条英文备注规则覆盖rule_covered、模型有输出、信息不足、失败、待处理分开；旧版本未记录时标unreported。automatic包括原分类映射及完整英文备注明确规则，均非人工确认。已处理不等于人工确认，失败不补零标签或猜测原因。CLI导入后自动加工；MCP查询仍不触发加工。正在处理新来源时可能暂报analysis_stale，应稍后重新固定原账单与分析版本。
