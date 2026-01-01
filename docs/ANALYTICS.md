# 分类层级与多维交叉分析契约 1.0

分析层把已发布标准账单重新整理成可追溯的事实表，支持三级分类，以及月份、渠道、场景等一至三个维度的交叉统计。它是可重建的派生发布；原始 `category`、金额、原件、账单身份和已接纳修订仍由基础资料库管理。

## 一笔一事实和分类依据

每条标准账单在分析发布中恰好对应一条事实，完整保留标准字段，再添加 `dimensions`、`classification`、`source_fingerprint`、`label_basis` 和 `proposed_labels`。`dimensions` 供筛选/分组；`classification` 保留证据、来源和状态；`source_fingerprint` 绑定这次分类实际使用的来源记录。`label_basis` 记录该条事实复用的固定标签版本、其基础发布和输入指纹；无可复用标签时为 null。查询明细另附 `trace`，可沿 `source_snapshot_ref` 和记录 ID 回到基础明细及原件。

`category_l1` / `category_l2` / `category_l3` 是分析目录中的稳定节点 ID，不把显示名称当身份。目录包含节点名称、层级和父节点。原分类与明确规则能证明的层级才填值；没有三级依据时保留上层，并将下层设为 null。不能为了让树看起来完整而把一级分类复制成三级。

分类维度的状态包括：

| 状态 | 意义 |
| --- | --- |
| `known` | 有当前规则或生效复核依据，维度为单值 |
| `unknown` | 没有足够依据，维度为 null |
| `ambiguous` | 存在不能唯一消解的候选或冲突，维度为 null，证据保留 |
| `not_applicable` | 该分类维度不适用于这条记录，维度为 null |

同一账单即使含多个候选物品或渠道，也不会展开为多笔金额。模型 `proposed` 候选不会直接进入生效维度、分组和统计；需要按原有标签复核流程确认。规则命中并不表示人工确认，分类覆盖率也不等于分类准确率。多商品账单目前不分摊商品金额，不能由备注里的多个商品推算各自消费额。

## 重新处理、发布和历史

```powershell
# 预览分类变化、覆盖及金额对账；不切换当前分析发布
python -m qianji reprocess

# 核查同次预览报告，再使用返回的 plan_ref 应用
python -m qianji reprocess --apply --expected-plan "预览返回的 plan_ref"
```

手工应用会重新核对来源发布、标签发布、词表和加工配方；预览后输入变化需重新预览。此流程不更改基础账单金额、原分类或基础 `generation`。基础算法本身升级仍使用 [REBUILD.md](REBUILD.md) 的 `rebuild` 流程。

默认基础 `export` 和标签发布后会尝试刷新分析层。基础发布无需等待模型；模型稍后产生候选时也不会把未确认结果算入指标。自动刷新失败不能解释为空账单，基础发布仍保持可用，分析快照的版本与新鲜度供调用方核对；可通过手工 `reprocess` 恢复。

每次分析发布固定以下身份：

| 字段 | 意义 |
| --- | --- |
| `analytics_ref` | 分析发布完整字节的 SHA-256；后续查询必须携带 |
| `source_snapshot_ref` | 完整基础 generation 与发布字节摘要 |
| `analysis_ref` | 此次使用的标签发布摘要；没有可用标签时为 null |
| `analysis_source_snapshot_ref` | 标签发布自身使用的基础账单版本；与当前事实基底可以不同，没有标签时为 null |
| `label_source_state` | `matched` 表示两个基础版本相同；`partial` 表示按每条输入指纹复用旧标签；`absent` 表示没有标签发布 |
| `taxonomy_version` | 分类词表版本 |
| `projection_version` | 分析事实构建算法版本 |
| `recipe` | 分类加工配方摘要 |

导入新账单后，旧账单的输入指纹未变时继续使用原标签和人工复核，避免暂时恢复已否定的规则。新增或已变更的账单用明确规则重新计算；`quality.label_coverage.reused/fallback` 分别记录复用和回退条数。`partial` 不表示把整个旧标签库直接套到新来源，单笔复用都有 `label_basis` 可核对。

历史分析发布不随 current 改变。读取旧 `analytics_ref` 时重放指定版本，不能静默换成最新。旧文件损坏、缺失或与引用不符时明确报错。快照 `freshness` 分别报告基础、标签及配方是否仍匹配当前版本；旧发布仍可读取，但调用方不能把它说成已覆盖最新导入。catalog 是当前实现的静态目录，使用历史结果时应核对其 `taxonomy_version`。

## HTTP 入口

沿用 `python -m qianji serve` 的本机只读服务及 Host/Origin 限制。分析接口从已发布文件读取，不从当前数据库临时拼接，不触发加工。原 `/api/v1/snapshot`、`/api/v1/datasets/*` 和 `X-Qianji-Contract-Version` 的兼容行为保持不变；分析接口使用独立 `contract_version: "1.0"`。

| GET 路径 | 参数及返回 |
| --- | --- |
| `/api/v1/analytics/snapshot` | 无参数；`available: false` 表示尚无分析发布；成功时返回固定身份、条数、质量和 freshness |
| `/api/v1/analytics/catalog` | 无参数；当前分类节点、维度、筛选白名单、分页上限和指标定义 |
| `/api/v1/analytics/facts` | 必需 `analytics_ref`；可选 `filters`、`offset`、`limit`（1–50，默认 20） |
| `/api/v1/analytics/cube` | 必需 `analytics_ref`；可选 `group_by`、`filters`、`offset`、`limit`（1–100，默认 30） |
| `/api/v1/analytics/quality` | 仅必需 `analytics_ref`；固定发布的分类覆盖与发布质量 |

`filters` 为 URL 编码的 JSON 对象，值通常为字符串；可空维度可传真正 JSON null。重复 JSON 键、NaN、非对象、未知筛选、重复 URL 参数均拒绝。`offset` 为 0–1,000,000 的整数。`group_by` 是逗号分隔的 1–3 个不同维度，省略时按 `category_l1,category_l2,category_l3` 分组。

支持的分组维度为 `month`、`week`、`weekday`、`date_local`、`kind`、原始 `category`、`asset_id`、三级分类、`channel`、`context`、`meal`、`merchant`。`ledger_id` 和 `currency` 始终自动加在分组键前，不占这三个分析维度名额。月份是来源本地日期的 `YYYY-MM`，周是 ISO 周年/周次 `YYYY-Www`，星期为字符串 `1`（周一）至 `7`（周日）。

筛选继承基础日期、账本、币种、类型、原分类、账户、记录编号、备注和金额范围；另支持分析目录中的派生维度，以及 `category_l1_state` 等七个分类维度的状态筛选。例如 `{"category_l3":null}` 包括所有三级维度为空的记录；再加 `"category_l3_state":"unknown"` 才限定未知，而不混入歧义或不适用。

facts、cube 和 quality 使用统一返回外壳：

```json
{
  "format": "qianji-analytics-query",
  "contract_version": "1.0",
  "analytics_ref": "固定分析发布摘要",
  "source_snapshot_ref": "固定基础发布引用",
  "analysis_ref": null,
  "analysis_source_snapshot_ref": null,
  "label_source_state": "absent",
  "taxonomy_version": "实际词表版本",
  "projection_version": "实际事实构建版本",
  "recipe": "实际配方摘要",
  "dataset": "facts 或 cube 或 quality"
}
```

此处为结构示意，引用须使用真实完整返回值。facts/cube 另含 `filters`、`matched_records`、`coverage`、`data`、`pagination` 和固定查询 `citation`。`pagination` 包含 `offset`、`limit`、`requested_limit`、`returned`、`total`、`next_offset`；只有 `next_offset: null` 才表示读完。cube 的 `total` 是分组数；`matched_records` 是完整筛选范围的账单笔数。

cube 每组有 `trace.filters`，可原样交给同一 `analytics_ref` 的 facts 查询以核对构成账单。每条事实的 trace 可继续定位原件。查询引用包含固定发布与筛选/分组，不因分页变化。

请求错误返回 400；固定版本不符返回 409；发布缺失、损坏或不可读取返回 503。空数组只在成功查询中表示固定发布的筛选范围没有记录；不能用捕获异常返回空数组的方式掩盖失败。

## 精确指标与缺失

所有金额使用货币主单位的两位小数字符串，计算使用 Decimal。账本与币种分开，禁止跨币种求和。事实同笔只计一次，即使一个分类维度未知或冲突，也保留在 null 分组，保证与基础账单对账。

| 指标 | 定义 |
| --- | --- |
| `expense`, `income`, `refund`, `transfer` | 各类型原金额合计 |
| `*_count` | 各类型事实笔数 |
| `count` | 全部四种类型事实数，不能统称消费次数 |
| `net_expense` | 支出减退款 |
| `net_cashflow` | 收入加退款减支出，不是余额 |
| `avg_expense` | 支出额除支出笔数；无支出笔数为 null |
| `expense_share_percent` | 本组支出除同筛选下本账本/币种全部支出，百分数；分母为零时为 null |

均值和占比按两位 `ROUND_HALF_UP` 舍入。退款按退款自身日期和分类统计，不倒挪到原消费；转账单列。真实零元账单可以产生 `0.00`，未知分类不能补零金额。没有记录的日期不凭空补成已核实零消费。

cube 的 `totals`、facts/cube 的 `coverage` 和 `matched_records` 都针对完整筛选范围；翻页时重复返回，不能逐页相加。quality 的 `publication_quality` 是构建发布时的对账与覆盖信息，`coverage` 反映这份发布中的事实，不声称现实全量或手机最新。

## 下游独立客户端

`integrations/analytics_client.py` 只使用 Python 标准库，可以复制到下游。客户端验证外壳版本、八个固定身份/状态字段、分页进度和完整条数，整次收齐才返回；中途失败不会交付部分结果。

不可变文件格式为 `qianji-analytics`，机器结构见 [../integrations/analytics.schema.json](../integrations/analytics.schema.json)，权威定义为独立客户端的 `analytics_schema()`。标准账单字段与基础 `snapshot_schema()` 同型；JSON Schema 描述分类决策、证据、标签基底、词表和质量字段。结构校验不能替代 SHA-256、固定来源、原文证据、分类父子关系和精确金额对账；生产方发布及读取时执行这些语义校验。

```python
from analytics_client import AnalyticsClient

client = AnalyticsClient("http://127.0.0.1:8790")
snapshot = client.snapshot()
if not snapshot["available"]:
    raise RuntimeError("尚无分析发布，需要先在来源项目加工")
ref = snapshot["analytics_ref"]

catalog = client.catalog()
report = client.cube_all(
    ref, group_by=["month", "category_l2", "channel"],
    filters={"start_date": "2026-01-01", "end_date": "2026-12-31"},
)
facts = client.read_all(ref, filters={"category_l3_state": "unknown"})
quality = client.quality(ref)
```

`read_all` / `cube_all` 返回带身份、完整 `data` 和查询元信息的对象，不是基础 `qianji-normalized` 全量文件。下游应将固定身份、筛选条件与结果一起保存。多份结果必须全部读取成功后再共同应用；即使期间新 current 发布，旧 ref 的读取仍固定不变。

## MCP

新增 `qianji_analytics_snapshot`、`qianji_analytics_catalog`、`qianji_analytics_facts`、`qianji_analytics_cube`、`qianji_analytics_quality`，全部只读。MCP 的 filters 是对象、group_by 是字符串数组，无需手工 URL 编码。facts 默认 5 条，cube 默认 10 组；响应超出 16,000 字符时减少整条/整组，保留全部字段及正确 next_offset。一条仍超限会明确失败，不截断证据或长备注。

先获取 analytics_snapshot 再固定 analytics_ref；分类树节点与查询参数查 analytics_catalog。需要看到原始完整未知字段时，使用基础 MCP 的 original 工具按事实的 source_snapshot_ref 和 ID 回溯。

成功发布分析后自动回收旧的标签/分析 JSON 副本。保留当前标签、当前分析及当前分析实际引用的标签，替换后的一小时内继续保留旧版本供在途读取；过期引用需重新获取当前输入。清理与两类发布共用进程锁，依赖文件缺失或摘要不符时停止清理。不会改写固定文件内容及时间、基础账单、来源归档、基础发布、评估历史或人工复核。基础发布仍是 labels.sqlite3 中保存评估的来源证据，不能按无用缓存直接删除。
