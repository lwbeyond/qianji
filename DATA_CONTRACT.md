# 钱迹标准化数据接口 · v1（契约 1.0 / 1.1）

生产方是本项目，消费方可以是你的其他本地程序（例如日历/日记类应用）。接口仅提供财务记录参考，不直接写入消费方资料库，不把账单生成日历活动或推断活动时长。

## 交付格式与身份

完整文件为 UTF-8 JSON：

```json
{
  "format": "qianji-normalized",
  "format_version": 1,
  "snapshot": {
    "generation": "资料库UUID:修订号",
    "library_id": "资料库UUID",
    "revision": 2,
    "updated_at": "2026-01-03T04:30:00+00:00",
    "contract_version": "1.1",
    "processing": {
      "normalizer_version": "qianji-normalize/1",
      "summary_version": "qianji-summary/1",
      "source_revision": 1,
      "source_updated_at": "2026-01-02T04:30:00+00:00",
      "processed_at": "2026-01-03T04:30:00+00:00"
    },
    "ledgers": [{"id": "main", "utc_offset": "+08:00"}],
    "available_datasets": ["transactions", "daily_summary"],
    "data_sha256": "datasets的SHA-256",
    "counts": {"transactions": 1, "daily_summary": 1}
  },
  "datasets": {"transactions": [], "daily_summary": []}
}
```

上述为结构示意，条数和校验值需以实际文件为准。消费方必须检查版本。`data_sha256` 对 `datasets` 按 Python `json.dumps(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)` 得到的 UTF-8 字节计算。此校验用于发现损坏，不证明来源可信。

消费方身份应使用 `(library_id, id)`；记录 `id` 为 `qj_` 加上 `[ledger_id, source_key]` 的上述规范 JSON 的 SHA-256。账本编号是调用者给定的稳定标识；源 `key` 以字符串原样保留。不同账本不因 `key` 相同而合并。同一资料库复制会保留 `library_id`。`generation` 始终等于 `library_id + ":" + str(snapshot.revision)`，重复导入不改变它。

历史原生契约 **1.0** 没有 `processing`：`snapshot.revision` 随实际新增、接纳修订或首次登记账本递增，`updated_at` 为该次本库变化时间。旧发布保持此口径，不向旧文件补字段。新版 HTTP 还提供无 processing 的 1.0 兼容视图，其 revision 和 updated_at 保留完整发布的交付口径，见下文 HTTP 协商；不能用兼容视图中的 revision 代替精确的来源修订计数。

契约 **1.1** 必须具有上述五个 `processing` 字段，禁止额外字段；契约 1.0 禁止出现 `processing`。两者的 `format_version` 仍为 `1`，HTTP `api_version` 仍为 `"1.0"`。新版独立客户端支持两种契约；已复制到下游、只认 1.0 的文件客户端需先更新才能直接读取 1.1。已有 HTTP 消费方可继续使用默认 1.0 兼容视图。

| 字段 | 契约 1.1 语义 |
| --- | --- |
| `snapshot.revision` / `generation` | 交付修订；实际来源变化或有效重建变化时递增。加工版本变化即使未改变 datasets，也产生新的交付版本 |
| `snapshot.updated_at` | 产生该交付修订的时间；重新 export 相同版本不刷新时间 |
| `processing.source_revision` | 本库累计接纳的来源变化序号，非负整数，不能超过交付修订；纯重建不递增 |
| `processing.source_updated_at` | 最近一次来源变化的接纳时间；纯重建不改变 |
| `processing.normalizer_version` | 生成当前标准化记录的非空算法版本名，例如 `qianji-normalize/1` |
| `processing.summary_version` | 生成当前每日汇总的非空算法版本名，例如 `qianji-summary/1` |
| `processing.processed_at` | 产生当前加工结果的时间，不能早于 `source_updated_at`；无变化重建不刷新 |

时间均需包含时区；它们不是钱迹原始编辑时间或账单发生时间。旧库迁移时来源序号和来源更新时间从原 1.0 元数据初始化；首次应用重建才添加加工版本并发布 1.1。新库直接发布 1.1。字段变化和金额语义变化仍需升级数据契约，不能仅修改加工版本名；`summary_version` 不授权改变本文件定义的收支公式。

导入记录缺失不代表删除，v1 不提供删除信号。源编号改变也不意味着旧记录已被替代。单条 `transactions.revision` 是从 1 开始的已接纳源修订，不随重建递增，不能超过来源修订序号（旧 1.0 使用 `snapshot.revision`）。这些序号都不是钱迹的全局时钟。

## transactions

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `id` | string | 稳定记录编号 |
| `ledger_id` | string | 调用者给定的账本标识 |
| `source_key` | string | 钱迹原始 `key` |
| `revision` | integer | 单条记录从 1 开始的已接受版本 |
| `occurred_at` | string | 加上配置时区偏移的 ISO 8601 时间 |
| `date_local` | string | 源本地日期 `YYYY-MM-DD` |
| `source_date` | string | 原始无时区日期文本 |
| `utc_offset` | string | 例如 `+08:00` |
| `timezone_basis` | string | 固定为 `configured_fixed_offset`，不是原文自带时区 |
| `kind` | string | `expense` / `income` / `refund` / `transfer` |
| `source_type` | string | 原始中文类型 |
| `amount` | string | 非负两位小数，不使用浮点数累计 |
| `currency` | string | 来源币种代码，不做换算 |
| `category` | string | 原始分类名称，不猜测层级或改名 |
| `remark` | string / null | 原始备注，保留内容与空值 |
| `asset_id` | string / null | 来源账户编号；字符串避免大整数精度丢失 |
| `from_asset_id` | string / null | 转出账户编号 |
| `to_asset_id` | string / null | 转入账户编号 |
| `related_source_key` | string / null | 原始非空 `sourceid` |
| `related_record_id` | string / null | 按同一账本映射的候选关联编号；不保证目标存在或来源语义完整 |
| `reimbursement_code` | integer / null | 原始 `hasbx`，不推断报销状态，不因此调整汇总 |
| `source_file_sha256` | string | 此版本首次接纳时的原件 SHA-256 |
| `source_row` | integer | 此版本原件数组中的位置，从 1 开始 |

日期按发生日期保留，不把退款挪回原消费日。转账为一条来源记录，不拆成两笔消费或收入。接口不暴露数据库路径、源文件名或未知原文字段；原始完整字节在本地归档，修订内部比较文档保留未知字段（其中 JSON 小数按精确字符串存储）。

transactions 排序为 `(date_local, occurred_at, id)` 升序。缺失与显式 `null` 等原文变化仍可产生修订；消费方必须允许部分字段为 `null`。

## daily_summary

每个 `(ledger_id, date_local, currency)` 一行，无账单的日期不生成行。所有金额为两位小数字符串：

| 字段 | 语义 |
| --- | --- |
| `ledger_id`, `date_local`, `currency` | 分组键 |
| `count` | 全部类型的账单数量，包括转账和退款 |
| `expense` | 支出原额合计 |
| `income` | 收入合计，不含转账与退款 |
| `refund` | 退款合计 |
| `transfer` | 转账金额合计，仅供参考 |
| `net_expense` | `expense - refund`，允许负值 |
| `net_cashflow` | `income + refund - expense`，不含转账，允许负值 |

`net_cashflow` 仅表示已导入账单的收支净额，不代表账户余额或资产变化。退款会计入对应退款日期，不要求关联支出已导入。不同币种不能直接相加。汇总排序为分组键升序。

## HTTP

监听 `127.0.0.1:8790`，可通过 `serve --port` 修改端口。仅本机后端读取，无跨域授权、无写入接口、无静态文件服务，不需要外部账号或网络。所有响应 `Cache-Control: no-store`。

| 路径 | 用途 |
| --- | --- |
| `GET /health` | 服务存活，不表示数据库完整性已验证 |
| `GET /api/v1/snapshot` | 返回 `api_version: "1.0"` 与 `snapshot` 元信息 |
| `GET /api/v1/datasets/transactions` | 标准化账单分页 |
| `GET /api/v1/datasets/daily_summary` | 每日汇总分页 |

HTTP 用 `X-Qianji-Contract-Version` 请求头协商快照元信息：

| 请求头 | 返回行为 |
| --- | --- |
| 不发送，或 `1.0` | 返回 1.0 兼容视图，去除 processing、contract_version 为 1.0；datasets、generation、revision、updated_at 与完整发布相同 |
| `1.1` | 返回完整发布元信息；发布已升级时为 1.1，尚未重建的旧发布仍为原生 1.0，不伪造加工元数据 |
| 其它值 | HTTP 400，明确拒绝未知请求版本 |

新版 `QianjiClient` 对每次请求发送 `X-Qianji-Contract-Version: 1.1`，仍接受忽略此头的旧服务返回的 1.0。1.0 兼容视图继续满足现有只检查 `contract_version == "1.0"` 的 HTTP 消费入口；它没有来源与加工时间的完整区分，需要该信息的消费方应显式请求 1.1。直接读取 current/versions 文件始终获得原发布内容，不进行协商或删字段。

数据集接口必须附 `generation`，值来自 snapshot；其余参数：

| 参数 | 规则 |
| --- | --- |
| `limit` | 1–5000，默认 500 |
| `offset` | 非负整数，默认 0 |
| `start_date`, `end_date` | `YYYY-MM-DD`，含边界，可分别省略 |
| `ledger_id`, `currency` | 精确匹配 |
| `kind`, `category` | 仅 transactions 支持，精确匹配 |

返回 `api_version`、`snapshot`、`dataset`、`data` 与 `pagination`。分页元信息含 `limit`、`offset`、`returned`、筛选后 `total`、`next_offset`（结束为 null）。snapshot 的 counts 是全库数量，不是筛选后数量。未知筛选项、重复参数、非法日期会报错，不静默忽略。

通常数据集与元信息在同一个数据库读取事务内取得。当前版本与请求不符返回 HTTP 409 `snapshot_changed`，不保留可继续分页的旧版 HTTP 快照；消费方必须重新完整读取。文件接口则在一次读取中取得完整的已发布快照。普通 CLI 导入后、文件发布前有短暂窗口，HTTP 可已看到更新，文件仍是此前完整版本；消费方比较 `generation` 即可识别。

基础重建另有待发布保护：候选已提交但默认发布尚未完成时，`Store.snapshot()` 和 HTTP 改读完整的 `exports/current.json`，MCP 也只读已发布文件，不把数据库候选当作当前交付。替换 current 之前旧版本继续可读；替换后读到完整新版本。待发布期间阻止新导入和再次重建，用默认 `python -m qianji export` 恢复；`--output` 的自定义导出不能解除此状态。

错误格式为 `{"error":{"code":"错误码","message":"说明"}}`，使用 400（请求无效）、403（非本机 Host / 跨站来源）、404（不存在）、405（写入方法）、409（版本改变）或 500（读取失败）。空数据仅在成功响应中表示筛选范围没有记录。

HTTP 当前实现针对本地个人规模，每页会读取当前完整记录以确保一致性；不是面向海量数据的服务。恢复备份前应停止服务，消费方在恢复后重新获取 snapshot。


## 机器校验与不可变发布

机器结构见 `integrations/snapshot.schema.json`（JSON Schema 2020-12）；权威定义为独立客户端 `snapshot_schema()`，测试校验 JSON 文件与定义一致。它约束类型、必需字段、契约与 processing 配套、金额字符串/小数精度、币种、枚举、日期时区和 UUID。

`validate_snapshot(doc, expected_library_id=...)` 校验内存中的完整交付，核验行数、datasets 摘要、generation 与库身份、账单/关联身份、来源修订边界、账本与日期/时区一致性，并以 Decimal 独立重算全部汇总，拒绝金额、笔数、分组或覆盖不一致。生产方在发布候选前使用同一校验。`load_snapshot(path, expected_sha256=..., expected_library_id=...)` 先校验完整文件摘要，再解析并调用该验证器。摘要正确不代表汇总正确，两层都需通过。复制客户端仍仅需一个 Python 文件，不依赖生产方包或额外校验库；它接受合法的未知算法版本名，不负责执行该算法。摘要是损坏检测，不是签名。

每次 export 在替换 current.json 前，保存 `versions/<sha256(generation)>.json`。文件字节不再覆盖；同 generation 再发布必须字节完全相同，否则失败并保留旧 current。CLI 返回快照字段和 `publication.path/sha256/retention`；publication.sha256 是完整文件摘要，区别于 snapshot.data_sha256。指定其他导出文件时，versions 建在该导出文件的相邻目录。

消费方把 generation、library_id、processing（如有）、publication 引用和完整文件摘要存入自己的分析输入清单，调用 `load_snapshot(path, expected_sha256=..., expected_library_id=...)`。仅看 data_sha256 不能区分「相同数据、不同加工版本」的交付。所有既有版本保留，不自动删除；磁盘清理由用户确认全部消费者已不再依赖后进行。旧快照文件没有逐条历史或原件，仍不能替代整个 data 的一致备份。

schema 2 迁移仅添加来源元数据，不重写旧发布和已接纳修订。重建应用前会把尚无固定 versions 文件的旧 current 原样保全；重建更新当前 `records.normalized_json`，不改 `revisions` 中的历史记录。重建计划、字段差异、运行留档和恢复流程见 [docs/REBUILD.md](docs/REBUILD.md)。

HTTP 保留版本变化 409 的行为，未新增历史分页服务。`QianjiClient.read_batch({"transactions": filters, "daily_summary": filters})` 收齐所有表后才返回；后表或后页 409 时整个调用失败，禁止部分应用或把异常变为空列表。返回 qianji-query 明确为筛选结果，不能当完整 qianji-normalized 全量替换。

验证命令为 `python -m unittest discover -s tests -v`。测试使用合成数据，覆盖两种契约、processing 字段约束、精确汇总、基础重建、历史保留、失败恢复和分页一致性；执行结果以当前测试输出为准，不以文档证明真实资料库已完成重建。

## 分类事实与交叉指标的独立契约

分析交付使用独立的 `qianji-analytics-query` / `contract_version: "1.0"`，完整定义见 [docs/ANALYTICS.md](docs/ANALYTICS.md)。它不更改上述基础契约 1.0/1.1，不覆盖原分类，也不把分析筛选结果标为基础全量文件。

`GET /api/v1/analytics/snapshot` 获取固定 `analytics_ref`；`catalog` 提供当前分类节点、维度与指标机器目录；`facts`、`cube`、`quality` 必须携带 `analytics_ref`。统一外壳另含 `source_snapshot_ref`、可空 `analysis_ref` / `analysis_source_snapshot_ref`、`label_source_state`、`taxonomy_version`、`projection_version` 与 `recipe`。标签基底可以早于事实基底，但只能按逐条输入指纹复用，且保留 `label_basis`。这些接口读取不可变分析发布，旧引用可跨 current 更新继续分页，与原基础 HTTP 只读取当前 generation 的行为分开。

分析发布的机器 schema 为 `integrations/analytics.schema.json`，由 `integrations.analytics_client.analytics_schema()` 生成；基础字段类型与原 snapshot schema 一致。发布文件 format 为 `qianji-analytics`，查询结果 format 为 `qianji-analytics-query`，不可混同。schema 是结构约束，精确对账、证据有效性和来源指纹需同时通过生产方语义校验。

事实保留每条标准账单，再添加维度、分类依据与来源指纹；一笔一事实，未知/歧义维度为 null，多值不展开金额。cube 支持 1–3 个不同维度，账本和币种始终隔离，指标沿用上述收支公式。原退款日期、转账边界和金额字符串语义不变；缺失不补零，模型 proposed 不入生效统计。事实与汇总均可追溯至固定基础发布及原件。
