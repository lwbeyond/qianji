# 基础数据重建与加工版本

本项目从钱迹原件接收账单，向其它项目交付标准化记录和精确汇总。`rebuild` 用已接纳来源重新执行基础加工，解决修复解析或标准化代码后，重复导入相同文件被去重、已有结果无法更新的问题。规则和模型标签另由 `data/analysis/` 管理，基础重建不依赖模型。

## 预览和应用

在项目目录运行：

```powershell
python -m qianji rebuild
```

默认只预览。程序固定当前资料库状态，找到每条记录最新已接纳修订所引用的归档文件和数组行号，核验原件摘要、已接纳原文和稳定身份，生成当前算法版本的候选记录与汇总。不会扫描归档后按文件日期或行顺序选择版本，也不会重新接纳被拒绝或已被替代的源记录。

输出包含 `plan_ref`、`generation_before`、`target_processing`、来源修订、记录数、变化统计、校验结果、`diff_path` 和 `report_path`。检查 diff_path 中逐条字段的 before/after；每日汇总变化时，也保存完整的汇总前后结果。候选先在内存中生成和校验，差异与运行报告写到磁盘。

```powershell
python -m qianji rebuild --apply --expected-plan "预览返回的 plan_ref" --no-analysis
```

应用会重新生成并校验候选。plan_ref 绑定当前基础数据、目标加工版本和确定性结果；预览后来源、当前结果或加工版本改变时会拒绝，需重新预览。该校验避免把此前看到的差异应用到另一份数据，不能当作来源签名。

`--no-analysis` 仅跳过完成后的可选标签调度。省略它时，CLI 会在有变化的基础重建发布完成后排队增量标签任务；不等待模型推理。预览、失败和 `unchanged` 状态不排队。底层 `Store.rebuild()` 不自动启动分析进程。

使用其它资料目录时，把参数放在子命令前：

```powershell
python -m qianji --data-dir "私密资料目录" rebuild
```

预览会留下 processing 运行报告和差异文件，不是完全无磁盘写入；它不修改源文件、已接纳账单、标准化结果或发布。首次通过 Store/CLI 打开旧资料库还会执行下述元数据迁移。

## 来源、加工和交付的版本

| 层次 | 标识及更新方式 |
| --- | --- |
| 单条来源 | `transactions.revision`；新增从 1 开始，显式接纳原文修订才增加，重建不增加 |
| 全库来源 | `processing.source_revision` / `source_updated_at`；实际新增、接纳修订或首次登记账本时更新，重建不更新 |
| 加工实现 | `processing.normalizer_version` / `summary_version`；指明生成标准化记录及汇总的算法，`processed_at` 记录对应加工时间 |
| 对外交付 | `snapshot.revision` / `generation` / `updated_at`；来源变化、加工结果变化或加工版本变化都会产生新修订 |

因此，同一批原件用新算法重建后，可以有相同的 source_revision、不同的 generation。仅加工版本变化、金额等结果完全相同时，也需要新交付版本以保留加工出处；内容和加工版本都未变则返回 `unchanged`，不增加 generation、不刷新 processed_at。消费方应保存完整 generation 和加工元信息，不能只凭 datasets 的摘要判断加工版本。

当前标准化实现 `qianji-normalize/1` 冻结在 `qianji/normalization_v1.py`，汇总实现 `qianji-summary/1` 冻结在 `qianji/processing.py` 的版本注册表中。旧发布没有 processing 时，原件追溯固定使用这两个 v1 实现，不能改用日后新的默认算法。

## 旧库、历史和下游兼容

Store 打开旧 schema 1 资料库时升级到 schema 2，仅补来源序号、来源更新时间等元数据。旧 current、versions、原件和源修订历史不会因为打开资料库被重写；MCP只读发布，无需打开或迁移 SQLite。

旧资料库首次应用 rebuild 会补齐 processing 并发布契约 `1.1`，即使账单字段未变化，也需要一个新 generation。新资料库直接使用 1.1。`format_version` 保持 1，HTTP API 版本保持 1.0。

文件接口交付完整发布；新版 `integrations/qianji_client.py` 同时接受 1.0 和 1.1。下游若复制过只认 1.0 的文件客户端，应在切换前更新该单文件客户端。已有 HTTP 消费方默认仍得到 1.0 兼容视图：只省略 processing 并标为 1.0，账单、汇总、generation、revision 和 updated_at 保持一致。新版客户端自动发送 `X-Qianji-Contract-Version: 1.1` 获取完整元信息；尚未升级的服务或发布仍可返回原生 1.0。未知协商版本返回 HTTP 400。

重建只替换 `records.normalized_json` 这一当前加工结果，不改记录身份、源修订、原件引用、raw_json 或 `revisions` 中当时接纳的历史记录。`history` 继续返回当时保存的结果，不把历史伪装成当前算法重算后的结果。

应用候选前，若旧 current 没有对应的 versions 文件，会先按旧 current 原始字节保全固定版本。已有固定发布不覆盖，同 generation 若已有不同字节会失败。旧发布和原件保留后，MCP 可按各自冻结的加工版本追溯；未知算法实现会明确失败。独立文件客户端不执行算法，只校验合法版本名、结构和数据一致性。

## 校验、记录和中断恢复

候选必须通过完整契约校验：字段与类型、条数、摘要、账单/关联身份、源修订边界、账本与日期时区一致性，以及全部 daily_summary 的 Decimal 精确重算结果。重建同时验证每条最新已接纳记录的原件字节、行号和原文比较文档，身份或原件不一致就停止，不静默丢弃记录。

```text
data/processing/
  diffs/<run_id>.json    plan_ref、字段差异、变化的汇总前后值
  runs/<run_id>.json     本次计划、变化统计、校验、状态、错误或发布引用
  last-run.json          最近一次运行报告
```

这些文件可能含私密字段值，与整个 data 目录一起排除在版本控制之外。运行报告常见状态：

| state | 含义 |
| --- | --- |
| `preview` | 候选和差异已校验，尚未应用 |
| `unchanged` | 应用校验通过，内容与加工版本均无变化 |
| `completed` | 数据库重建和默认发布均完成 |
| `failed` | 本次重建未提交；查看 error 和校验结果 |
| `candidate_prepared` | 已保存候选恢复信息，尚未确认数据库提交 |
| `interrupted` | 状态检查确认候选在提交前中断，未应用 |
| `publication_pending` | 数据库候选已提交，默认发布或收尾尚待恢复 |

`python -m qianji status` 展示 rebuild 记录和 `publication_pending`，优先显示数据库标记的待恢复任务，避免后来失败的预览掩盖它。提交前恢复回执记为 candidate_prepared、applied=false；状态检查结合 SQLite 提交标记判断为 interrupted 或 publication_pending，不把中断回执当作已完成。

候选提交前出错会回滚数据库，旧发布继续有效。提交后若替换 current 失败，旧 current 继续可读；等待恢复期间 `Store.snapshot()` 和 HTTP 改读已发布的完整文件，MCP原本就读取文件。即使中断发生在原子替换之后，也只会读到完整的新发布，不会读到半份候选。

```powershell
# 恢复默认发布；可以不启动模型标签任务
python -m qianji export --no-analysis
python -m qianji status
python -m qianji verify
```

等待恢复时阻止新导入及再次重建，避免把另一批来源变更混入候选。默认 export 会发布已提交候选、完成报告并清除 pending 状态；`export --output ...` 仅写自定义目标，不解除默认发布待恢复状态。不要通过重导旧文件或手工删除元数据来绕过恢复。

该恢复入口不替代备份：一致备份仍应保存完整 data 目录，包含原件、业务库、固定发布、processing 报告及需要保留的 analysis 历史；单个快照或运行报告不能恢复全部来源历史。

## 后续修改加工算法

1. 为变化新增算法实现，在 `NORMALIZERS` 或 `SUMMARIZERS` 注册新版本；保留旧版本函数和注册项，不在同一个版本名下修改历史语义。
2. 更新 `NORMALIZER_VERSION` 或 `SUMMARY_VERSION`，为变化添加合成样本，验证候选、精确汇总和旧固定发布的原件追溯。
3. 在目标资料库先运行 rebuild，检查差异，再按 plan_ref 应用。目标算法版本与库内版本不一致时，导入返回 `processing_upgrade_required`，需先完成重建，避免当前库混用算法。
4. 完成发布后再继续导入。下游以新 generation 刷新；若需要标签，再按来源新版本增量更新分析。

算法版本用于标识实现变化，不允许绕过对外契约。字段增删、类型变化或业务语义变化仍需升级数据契约及消费校验；特别是 summary 版本升级不能擅自改变支出、退款、转账的定义或净收支公式。现有契约要求每个账本/日期/币种单独汇总，不进行浮点累计或跨币种相加。

验证入口为 `python -m unittest discover -s tests -v`，测试使用合成数据。真实资料库是否已重建，应以实际运行报告和发布中的 processing 为准；本文不记录或推定某次真实重建已经完成。
