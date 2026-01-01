# 钱迹原字段与增量标签

导入后可自动进行来源侧增量标签处理，原分类是主分类，英文备注用于补充细分信息。标签加工不改原分类、备注、金额、交易类型或业务 SQLite；分析结果位于本项目 `data/analysis/`。

标准化账单和确定性汇总是核心数据交付，标签是可选增强。基础数据重新加工使用 `python -m qianji rebuild`，详见 [docs/REBUILD.md](docs/REBUILD.md)；本文件的 `process` 和 `batch_analysis` 只更新派生标签。基础重建不调用模型、不改标签或复核库；CLI 在有变化的重建发布完成后默认另外调度标签任务，可用 `rebuild --apply --expected-plan "plan_ref" --no-analysis` 跳过本次调度。模型缺失、离线或候选失败不会阻止基础数据发布。

## 日常使用

正常运行 `python -m qianji import 文件` 或拖入 `import-file.cmd`，导入并发布成功后会自动排队。后台先发布规则结果，再处理需要模型补充的备注并发布新结果；导入不等待推理完成。默认使用本机已有 `qwen3.5:9b-32k`，不自动下载模型。

任务持久化于 `data/analysis/jobs.sqlite3`。连续导入合并到最新来源版本，单个后台消费者串行处理；操作系统文件锁在进程异常退出后释放。暂时离线/模型未安装时保留任务，15 秒开始退避，最多间隔 5 分钟重试；不会把连接失败计入每条备注的校验失败上限。任务完成后进程退出。电脑重启或后台进程被强制结束后，队列保留，下次导入/默认 export 自动续跑；本项目未安装开机任务或系统服务。

`python -m qianji status` 或 `python -m qianji.auto_analysis status` 查看状态、待处理请求、覆盖和安全错误码。状态中的 running 是最近记录，不保证进程仍存活。特殊维护时可运行 `python -m qianji.auto_analysis enqueue` 唤醒队列。`import/export/rebuild --no-analysis` 仅跳过本次触发；导入预览、重建预览、无变化重建、失败导入、指定自定义输出的 export、HTTP/MCP 查询不会排队。底层 `Store.import_file`/`Store.export`/`Store.rebuild` 是数据层接口，不自行启动后台进程，集成方应使用 CLI 或发布成功后显式调用 enqueue。

重建后的交付 generation 可能变化，而单条账单的已接纳源修订不变。分析发布绑定完整来源版本，旧分析此时会明确报 `analysis_stale`，需要增量更新后重新固定分析引用。相同有效输入与配方仍可复用已有结果；字段变化是否需要重新推理由输入指纹决定，不能把「基础重建」等同于「全库重新调用模型」。若选择 `--no-analysis`，过期标签不会自动更新，基础账单仍可正常读取。

原分类继续作为账单主分类。规则补充原分类别名和完整英文备注，例如 lunch→午餐、midnight meal→加餐。仅忽略大小写和连续空白，保存原文证据；不做子串或模糊匹配。water、phone、subway 等有歧义的词需原分类上下文。否定、多个商品、含商家/场景的长备注交给模型。整条备注被规则覆盖时不调用模型，状态为 rule_covered；已有模型历史保留，但当前展示由规则及人工复核决定。

规则标签 automatic 自动生效，无需逐条确认。复杂备注的模型输出仍为 proposed；本轮实测尚不足以证明任意新备注可无复核自动采纳。原分类的汇总不依赖候选是否被接受。

可在私有 `data/analysis/config.json` 覆盖后台配置（不存在时使用以下默认值）：

```json
{"enabled":true,"model":"qwen3.5:9b-32k","think":false,"num_ctx":8192,"num_predict":1536}
```

enabled=false 停止后续排队及后台重试；正在进行的单次推理/当前轮可能先结束。模型、思考开关、提示词、结构和采样参数参与配方指纹。更改后下次导入/排队生效，已复核项保留。已完成且配置未变的任务无需连接 Ollama；同名模型权重变化在下次需要模型时核验。直接 CLI 小批还可使用 `--think` 做显式思考实验（输出预算3072）；后台配置需同时设置合适的 num_predict。

双击 `process-analysis.cmd` 仍可手动处理最多 **20 条**需要模型的备注；已有完整规则覆盖、当前配方已处理或已复核项跳过。原件导入/发布继续按来源既有流程，该维护命令不采集、不导入、不重新发布原账单。

```powershell
python -B -X utf8 -m qianji.analysis audit
python -B -X utf8 -m qianji.analysis process
python -B -X utf8 -m qianji.analysis process --model qwen3.5:9b-32k --model-limit 20 --start-date 2026-01-01 --end-date 2026-01-31
python -B -X utf8 -m qianji.analysis status
```

不带 `--model` 只运行规则。换资料目录时全局 `--data-dir` 放在子命令前。模型只处理有备注的支出，优先较新且规则未命中的记录；退款、收入、转账不套用消费备注模型。候选标 `proposed`，默认不进入生效标签筛选。

推理固定本机 `127.0.0.1:11434`，禁用代理/重定向，只传原分类和备注，不传金额、账户标识或 expense 类型词。默认8192上下文、1536输出预算、关闭思考、temperature=0、presence_penalty=0、repeat_penalty=1；按输入字节保守估计预算，过长拒绝、不截断。模型只补充备注信息，使用扁平标签键和独立商家数组；信息不足由空数组决定，去掉冗余布尔值。错误细分为 JSON、结构、标签、引文、重复等安全码。每条当前输入/配方累计校验失败3次后跳过，显式 `--retry-failed` 才再试；不会自动接受模型建议。

## 全库派生更新

需要全库加工时，可双击 `process-analysis-all.cmd`，或运行：

```powershell
python -B -X utf8 -m qianji.batch_analysis
```

全库是当前已发布记录，不含未导入或手机实时数据。规则更新全部记录，模型只处理有备注的支出。每批最多200个独立来源记录，已处理/已复核跳过；相同类型、分类、备注与模型配方复用预测，但每笔账单各自保留身份、修订和结果，不合并原记录。缓存种子不使用已复核记录；相同输入已存多种预测时不任意选取。

同一失败输入每轮只实际推理一次，每笔记录每轮只尝试一次，最多三轮，持久失败达到上限后停留待检查。确实要重试耗尽项时显式加 `--retry-failed`；仍最多三轮。不会覆盖复核或强制生成候选。正常中断后重新运行可增量继续；本机 Ollama 须已运行，勿同时安排大模型问答以免排队超时。

此手动入口的进度与最终结果写 `data/analysis/full-run-progress.json`，自动队列另存 jobs.sqlite3。`completed` 表示适用记录已有本配方结果、明确规则覆盖或复核；`completed_with_failures`/退出码2表示全范围已检查但仍有模型失败，不等于分类全对。`unresolved_records` 与 `failure_codes` 显示未完成模型校验的范围，`model_processed` 可包括信息不足结果。每批发布完整的派生读视图；来源版本变化立即停止，不拼接新旧账单。查询仍默认排除 proposed。

## 字段和分类

盘点核对当前发布引用记录的原件哈希、行号和全部标准化字段，再列顶层字段、非空/缺失覆盖及映射。钱迹 JSON 导出的顶层字段中，`key/type/money/date/currency/category/remark/asset/from/target/sourceid/hasbx` 有标准映射，其他字段（如 `billflag`、`username`）原样保留、不臆测语义。编号可精确筛选，不推断账户名称或保证关联完整。

盘点不覆盖未导入文件、被替代历史或嵌套子字段的全量目录。未知字段仍可按记录经 `qianji_original` / JSON Pointer 读取，未知代码不猜语义。

| 维度 | 用途及例子（合成示意） |
| --- | --- |
| item | 商品/服务：餐食、咖啡、茶饮、饮料、酒饮、交通、会员订阅等 |
| meal | 原文明确说到的早餐、午餐、晚餐、加餐 |
| channel | 明确的外卖、网购、线下 |
| context | 明确的通勤、出差、旅行、聚餐、学习、工作 |
| record_note | 平账、代付、报销、退款说明的文字标记，不判断实际完成状态 |
| merchant | 原文出现的商家，不把商品、人名或账户编号猜成商家 |

原分类明确映射生成 `automatic` 标签；以“平账（”或“平账(”开头的备注生成文字标记。不改账单类型、金额，也不默认剔除。英文品名/餐次可产生模型候选，证据保留英文原文。代码检查词表、精确引文及字段位置，但**引文存在不证明语义正确**。不判断必要、冲动、浪费、健康程度、动机或实际使用。

完整英文短备注还可由 `qianji/label_rules.py` 的显式整条映射直接生成 automatic 标签；命中规则会保留英文原文依据。复杂备注才走模型候选。

六维可重叠，不替换原分类、不强行统一层级。完整消费结构继续用原分类；细分使用独立标签集合。

## 查询与覆盖

MCP先 `qianji_snapshot` 固定 `snapshot_ref`，再 `qianji_analysis_snapshot` 获得匹配的 `analysis_ref`、词表和覆盖。服务器同时返回 `analysis_handle`，优先把这个短值填入labels的`analysis_ref`参数，避免模型抄错长哈希。短引用仅当前MCP进程有效，带随机前缀，最多缓存16组原账单/分析版本；重启、逐出或来源不匹配明确拒绝，重新取analysis_snapshot，不换成最新分析。完整哈希接口继续支持。`qianji_labels` 的用法：

- `tags:["item:咖啡","channel:外卖"]`；`tag_mode=all` 交集、`any` 并集、`not` 排除命中任一标签的记录，单笔只计一次。
- 日期等 `filters` 与原明细一致；分页固定两个引用和查询条件。
- 默认 `include_proposed=false`，仅规则/人工标签；明确预览才设 `true`。同时设 `only_proposed=true` 可只看含模型候选的记录。
- `review_table_markdown` 是代码生成的本页标签/逐字证据/状态表，区别规则生效与人工确认，不含金额或记录编号；`coverage_text` 明确筛选前后记录数。响应超预算时整条分页，表与data保持同一页。
- `totals` 精确统计全筛选结果，不是一页之和；账本/币种分开。退款按自身分类，不自动继承消费备注标签，标签净额不是已核对全部退款的最终成本。
- `coverage_before_tag_filter` 为日期等条件下、标签和候选记录筛选前的覆盖。`rules_processed/model_processed` 只表示当前输入/配方已处理，不代表有标签或判断正确。
- `with_stale_history` 表示保留旧输入或旧配方历史，**不等于当前标签全部过期**。逐条 `stale_reviews` 为输入更新后的旧复核记录，需要重新判断。
- `with_automatic_labels`、`with_confirmed_labels` 分别统计规则/人工覆盖，`with_effective_labels` 是两者并集；不能将生效数说成人工确认数。
- `model_status_counts`：不适用ineligible、已复核reviewed（可为拒绝）、整条备注规则覆盖rule_covered、模型有输出processed_with_labels、信息不足insufficient、失败failed、待处理pending；逐条analysis_status带状态及失败安全码。旧固定分析没有细分状态时为unreported，不当成零失败。model_processed可能保留本配方历史结果；rule_covered表示当前路由由规则负责。有模型输出不保证有可见proposed标签，也不代表被接受。

多次标签查询可能重叠，不能直接再相加。未处理、无标签、不命中均不证明现实中没有该类消费。候选预览的汇总必须标为未确认。原备注按返回id另读，证据和源哈希/行号/修订可追溯。

## 人工纠正与更新

`labels.sqlite3` 存来源身份/修订、输入指纹、规则/模型配方、模型摘要、引文和复核历史，不复制整份账单。源记录修订后不沿用旧标签；模型配方更新只重评未复核项。发布到不可变 `exports/versions/<sha256>.json`，原子更新 `current.json`；读取校验摘要、来源版本/身份、逐条输入和引文。源发布改变则旧分析报 `analysis_stale`，需重跑 `process`；MCP不悄悄加工。

人工接受优先于同维度规则/模型，明确拒绝在当前输入下跨配方保留，已复核记录不被模型覆盖。同维度可多标签，改判时需要拒绝旧标签、接受新标签；源修订变化后重新复核，历史保留。

暂无线下复核图形界面。以下是**合成格式样例**，必须用查询所得真实id/源修订、`status`最新`store_revision`和逐字证据替换，保存在本项目忽略的 `.local/review.json`：

```json
{
  "record_id": "合成示意，替换为真实id",
  "source_revision": 1,
  "expected_revision": 12,
  "request_id": "my-review-unique-001",
  "reason": "本人核对备注后纠正",
  "decisions": [
    {"dimension":"item","label":"酒饮","action":"reject"},
    {"dimension":"item","label":"饮料","action":"accept","field":"remark","evidence":"合成饮料"}
  ]
}
```

```powershell
python -B -X utf8 -m qianji.analysis review --file .local/review.json
python -B -X utf8 -m qianji.analysis publish
```

并发用 `expected_revision`；同一请求重试复用 `request_id`，同编号不同内容拒绝。复核只写派生库，显式 `publish` 后MCP才看到。纠正旧决定使用新请求记录相反决定，不删历史。没有向MCP提供写入/确认工具，也没有冒用用户身份自动接受模型标签。

## 备份与验证边界

```powershell
python -B -X utf8 -m qianji.analysis backup --out data/analysis/backups/labels-YYYYMMDD.sqlite3
```

SQLite在线一致备份不覆盖已有目标，并校验完整性。保留派生历史需备份分析目录；停止加工/复核/来源导入后按来源文档另行保全完整 `data`。仅标签库不能恢复原件、源修订或发布。合成测试覆盖独立恢复标签库并重建相同条目；真实备份完整性通过不等于整套真实恢复成功。

源旧版本留存仍受 [MCP.md](MCP.md) 约束，有固定分析文件不代表源快照一定能重放。基础重建应用前会由生产方保全旧 current 的固定版本。标签升级保留旧配方历史，应纳入来源备份容量管理；标签模块不自动清理或迁移业务库。基础数据 schema 2 的元数据迁移与重建是独立流程。

## 模型说明

默认模型为本机 Ollama 中已有的 `qwen3.5:9b-32k`（关闭思考）。配置也可指向其他支持 `/api/chat` 结构化输出的本机模型；模型、思考开关、提示词、输出结构和采样参数都参与配方指纹，更换后只重评未复核记录。小样本对照只能用于工程选型，不代表生产准确率；复杂备注的模型输出始终保持 `proposed`。

依据官方文档：[Ollama 结构化输出](https://docs.ollama.com/capabilities/structured-outputs)、[思考控制](https://docs.ollama.com/capabilities/thinking)。
