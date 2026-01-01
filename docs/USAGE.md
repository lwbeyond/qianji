# 使用说明（导入、去重、重建与读取）

这个项目负责钱迹导出文件的接收、标准化、去重、修订留档、基础数据重建和只读交付，没有前端。本地消费程序可以直接读取 JSON 快照，也可以通过本机 HTTP 接口读取；项目之间不共享数据库。标准化账单与精确汇总是基础交付，规则和模型标签是独立的可选增强数据。

需要 Python 3.11 或更新版本。运行时只使用标准库，无须安装依赖。

## 日常使用

本地消费者可使用独立的 [临时只读服务入口](../READER_SERVICE.md)，无需导入钱迹内部模块。

在项目目录打开终端：

```powershell
# 先查看新增、重复与修订数量（不会导入账单）
python -m qianji preview "导出文件.txt"

# 导入 .txt 或 .json，内容必须是钱迹 JSON 数组
python -m qianji import "导出文件.txt"

# 查看覆盖日期、记录数量，检查数据库和原件归档
python -m qianji status
python -m qianji verify
```

也可以将文件拖到 `import-file.cmd`。它导入到默认账本 `main`，显示结果后停留。成功导入会自动更新 `data\exports\current.json`，原件按 SHA-256 原样复制到 `data\archive`，并自动启动后台增量标签处理。导入命令无需等待模型完成；关闭导入窗口后后台继续。原分类保留，常见英文整条备注由明确规则补充，复杂备注由本机模型提出候选。普通导入不会移动或改写用户选择的源文件；初次整理的原始导出文件另存于 `data\inbox`。

`main` 是账本的稳定标识。以后重复导出同一个账本继续用 `main`；导入另一个账本必须指定另一个标识，例如 `--ledger travel`。不从可变的文件名或账本标题猜测身份。

## 去重和修订

- 以「账本标识＋钱迹 `key`」识别记录。金额、时间、备注相同但编号不同的两笔会分别保留。
- 同一编号、相同原文不产生新记录或新修订；JSON 对象键顺序、顶层账单排列和金额 `10` / `10.0` 的差别不算修订。
- 同一编号内容改变，默认阻止整批导入，返回冲突编号与变化字段。不会先导入其中一部分。
- 同一文件内同编号出现两个不同版本，直接报错，不以排列顺序选择版本。
- 导出缺失的旧记录继续保留，不推断删除。不自动识别编号变化后的同一笔，也不按相似度删除疑似重复。
- 不用账单发生时间判断版本新旧。旧导出再次导入也可能构成冲突，不能静默回滚新版本。

确认修订时先预览，检查 `conflicts`，再使用返回的 `generation_before`：

```powershell
python -m qianji preview "新版导出.txt"
python -m qianji import "新版导出.txt" --accept-updates --expected-generation "预览中的完整 generation_before"
```

这会接纳该文件的全部修订，保留原件和每条记录的历史。预览后如果资料库发生变化，接纳会失败，需要重新预览。可以用 `python -m qianji history "qj_记录编号"` 查看历史版本。CLI 中 `updated` 表示检测到的修订数量；只有 `applied: true` 才表示已保存。

## 基础数据重建

升级日期解析、标准化或汇总实现后，用 `rebuild` 从每条记录**最新已接纳修订所引用的原件**重新生成当前基础数据。它保留账单身份、源修订、原件与历史，不按归档文件的时间或排列重选账单版本。

```powershell
# 生成候选、校验精确汇总并记录字段差异；默认不应用
python -m qianji rebuild

# 检查返回的 diff_path，使用同次预览的 plan_ref 应用
python -m qianji rebuild --apply --expected-plan "预览返回的 plan_ref" --no-analysis
```

预览会写运行报告和差异文件，但不修改账单、原件或发布；首次打开旧库会做元数据迁移。应用时重新校验计划，数据或加工版本已变化则拒绝。新结果校验后才切换发布；内容与加工版本均无变化时不增加 `generation`。移除 `--no-analysis` 后，完成有变化的重建会调度可选标签更新，基础发布不等待模型。

来源修订与加工版本分别记录在 `snapshot.processing`，`generation` 继续标识完整交付版本。新库的文件发布使用契约 `1.1`；旧库仅打开时不改旧发布，首次应用重建才补齐加工元信息。下游若复制过只认 `1.0` 的文件客户端，升级发布前应替换为本项目新版 `integrations/qianji_client.py`，新版同时支持 `1.0` 和 `1.1`。HTTP 默认继续提供 1.0 兼容视图，保留已有 HTTP 消费方的版本入口。

计划、运行记录、失败恢复和算法升级步骤见 [REBUILD.md](REBUILD.md)。

## 分类层级与多维交叉分析

分析层提供三级分类、逐条证据与未知/冲突状态，并支持月份、渠道、场景等一至三个维度交叉。每笔账单保留一条事实，金额不随多标签展开；原分类保留，模型候选不直接进入统计。基础发布和标签发布后会尝试刷新，手工加工可先预览再按同次计划应用：

```powershell
python -m qianji reprocess
python -m qianji reprocess --apply --expected-plan "预览返回的 plan_ref"
```

统一只读接口位于 `/api/v1/analytics/{snapshot,catalog,facts,cube,quality}`。先读取 snapshot，再把 `analytics_ref` 固定到所有查询，历史发布可继续重放。`integrations/analytics_client.py` 为可复制的标准库客户端；字段、精确指标、null 语义、追溯和示例见 [ANALYTICS.md](ANALYTICS.md)。

## 给其他本地程序读取

推荐先用文件接口：每次导入成功后读取 `data/exports/current.json`，无需同时运行两个服务。该文件先完整写好再替换，读者不会读到半份文件。它包含标准化账单 `transactions`、按账本／日期／币种分组的 `daily_summary` 和资料库版本。

`integrations\qianji_client.py` 是可独立使用的标准库客户端。消费方后端可以将它加入模块搜索路径，或者复制到自己的代码目录。示例：

```python
import sys
sys.path.insert(0, r"<项目目录>/integrations")
from qianji_client import load_snapshot

document = load_snapshot(r"<项目目录>/data/exports/current.json")
transactions = document["datasets"]["transactions"]
daily_totals = document["datasets"]["daily_summary"]
generation = document["snapshot"]["generation"]
```

若需要日期筛选和分页，运行 `start-api.cmd` 或：

```powershell
python -m qianji serve
```

服务地址默认 `http://127.0.0.1:8790`，只允许本机连接，只读，不提供前端或上传入口。消费方从后端调用，不直接跨域从浏览器访问：

```python
from qianji_client import QianjiClient

client = QianjiClient()
rows = client.read_all("transactions", start_date="2026-01-01", end_date="2026-01-31")
totals = client.read_all("daily_summary", start_date="2026-01-01", end_date="2026-01-31")
```

新版客户端自动发送 `X-Qianji-Contract-Version: 1.1` 获取完整加工元信息；旧服务忽略此头时仍可读取 1.0。没有请求头的已有 HTTP 消费方得到 1.0 兼容视图：账单、汇总、generation 和 updated_at 相同，仅省略 processing 并使用 `contract_version: "1.0"`。文件发布不作此兼容投影。

一次读取固定一个资料库版本。读取过程中若导入或重建改变了交付版本，服务返回 409，客户端抛出异常；消费方应丢弃本次结果，创建新客户端并重新读取，不能把失败当成「没有账单」。`read_all()` 收齐并验证所有分页后才返回。跨多个数据集应先全部读取成功，再一起应用。

字段和汇总语义见 [DATA_CONTRACT.md](../DATA_CONTRACT.md)。

## 金额、日期与支持范围

金额精确处理，接口输出两位小数字符串；不做汇率换算，不同币种、账本分开汇总。转账不算收入或支出；退款在退款发生日期单列，并抵减净支出。不改写原支出发生日期。

当前支持的中文类型值：支出、收入、退款、转账。未知类型、无编号、日期错误、负数金额、有效小数超过两位等情况都会拒绝整批导入。未知扩展字段参与修订比较并保留在本地，不臆测其业务含义。账户名称、退款关联是否完整等信息不在该导出中保证提供；接口保留源账户编号和源关联编号。

原始时间没有时区，默认明确采用固定 `+08:00` 偏移。导入其他时间体系的账本时，在第一次导入指定 `--utc-offset=+00:00` 等值；同一账本不能在后续导入时更换偏移。这个版本不处理夏令时，也不会声称时区来自钱迹原文。

## 私密资料与恢复

```text
data/
  inbox/                 初次整理保留的原始文件名副本
  archive/<sha256>.json   原件字节，可能带 UTF-8 BOM
  qianji.sqlite3          当前记录、每次导入信息、账本配置、全部修订
  exports/current.json   给消费方的标准化快照
  exports/versions/      按 generation 固定的不可变发布
  processing/diffs/     基础重建的字段及汇总差异
  processing/runs/      每次重建的计划、校验与执行记录
  processing/last-run.json  最近一次重建记录
  analysis/             独立可选标签、复核与任务队列
```

以上全部留在本机，并被 Git 排除。`.gitignore` 采用代码和文档白名单，根目录新增的账单、报告等文件默认不会被提交。程序不会上传数据。归档不等于加密，也不等于异地备份；需要备份时，停止导入与服务后复制整个 `data` 目录到自己的备份位置。只复制快照无法恢复原件和修订历史。

如果导入成功但写快照时被其他程序占用，CLI 会明确提示「导入已保存，但快照发布失败」，运行以下命令重新发布即可：

```powershell
python -m qianji export
```

重建提交后若发布失败，`status` 显示 `publication_pending`。切换成功前旧 `current.json` 继续供读取；等待恢复期间 HTTP 与 `Store.snapshot()` 读取已发布文件，MCP也继续读发布文件，不暴露尚未发布的数据库候选。先运行默认 `export` 恢复，才能继续导入或再次重建；仅导出到自定义路径不会解除待发布状态。无需模型时可用 `export --no-analysis`。

切换资料目录时，把全局参数放在子命令前：`python -m qianji --data-dir "其他私密目录" status`。通过 CLI 或 `Store` 打开旧库会将数据库升级到 schema 2，仅添加来源元数据，不改旧发布和修订历史。`preview` 会初始化空资料库身份，但不导入账单、不存原件；MCP 读取发布无需打开业务库。

退出码：`0` 成功（预览含冲突也算预览成功）；`1` 格式、校验或系统错误；`2` 实际导入因未接纳修订而被阻止。

## 验证

```powershell
python -m unittest discover -s tests -v
```

测试只使用动态生成的虚构账单，覆盖原件字节归档、精确汇总、去重与修订、整批回滚、原件损坏检测、快照校验、基础重建与失败恢复、加工版本兼容、分页一致性和本机访问限制，不依赖个人导出文件。

## 源明细 MCP

已新增完整标准字段筛选、固定版本分页、单条原件追溯、原分类/日期精确汇总、原字段盘点和派生标签读取。安装、九工具与精度/留存边界见 [MCP.md](../MCP.md)。MCP只读已有发布和原件，不初始化业务库、导入或重新发布。

正常导入已自动触发增量加工，无需再双击分类脚本。`python -m qianji status` 会显示后台任务状态；细节可运行 `python -m qianji.auto_analysis status`。规则标签自动生效；未经验证的模型候选仍明确标为 proposed。派生标签、证据、任务队列和复核历史单独留在 `data/analysis/`。配置、异常恢复及研究结果见 [ANALYSIS.md](../ANALYSIS.md)。

`process-analysis.cmd`（小批）和 `process-analysis-all.cmd`（全库）保留为手动维护入口。自动任务与手动任务互斥，相同输入复用、已有复核跳过、失败有限重试。自动任务状态存于 `data/analysis/jobs.sqlite3`；旧全库入口另写 `full-run-progress.json`。MCP区分规则已覆盖、模型不适用、信息不足、失败、待处理和已复核；全库跑完不等于标签全部正确或已确认。
