# qianji-local

Local, auditable processing of **your own** [钱迹 (Qianji)](https://qianjiapp.com/) bookkeeping-app JSON exports: strict normalization, deduplication with revision history, deterministic rule-based classification into a frozen 3-level taxonomy, optional local-LLM *candidate* labels, exact analytics (facts / cubes / quality), a loopback read-only HTTP API and a read-only MCP server. Pure Python standard library for the core (Python 3.11+).

在本地处理**你自己导出的**钱迹账单 JSON：严格标准化、按编号去重并保留修订历史、用确定性规则把账单归入固定版本的三级分类、可选的本机模型**候选**标签、精确的分析事实/交叉统计/质量报告，以及仅本机可访问的只读 HTTP 接口和只读 MCP。核心只用 Python 标准库。

> This repository contains code, docs and synthetic tests only — **no bills, amounts, merchants, accounts or exports**. See [Privacy](#privacy--隐私).

## Pipeline overview / 处理流程

```text
Qianji JSON export ──► normalize (qianji-normalize/1) ──► SQLite: records + revisions ──► publication (exports/versions/<sha256>.json, current.json)
                                                                                     │
                       labels (qianji-labels-1): category map + exact remark rules ──┤ (+ optional local model → proposed only)
                                                                                     ▼
                       taxonomy (qianji-taxonomy/1) classify() ──► analytics facts (qianji-analytics/1) ──► facts / cube / quality (HTTP + MCP)
```

Every stage is **versioned and replayable**: publications pin `processing.normalizer_version` / `summary_version`, analytics pin `taxonomy_version`, `projection_version` and a `recipe` hash. Old publications are always re-read with their own version, never the latest. Source bills (amount, date, original category, remark) are never rewritten by labeling or analytics.

每一层都有版本号并可重放；标签与分析永远不改写原账单的金额、日期、原分类和备注。

## 1. Normalization / 标准化 (`qianji/normalization_v1.py`)

Input must be a Qianji **JSON array** export (`.json` / `.txt`, UTF-8, optional BOM, ≤128 MiB). The whole batch is rejected on any error — nothing is partially imported.

| Source field | Normalized | Rule |
|---|---|---|
| `key` | `source_key`, `id = "qj_" + sha256([ledger, key])` | required; identity is **ledger + key**, never amount/time/text similarity |
| `type` | `kind` | `支出→expense`, `收入→income`, `转账→transfer`, `退款→refund`; unknown types rejected |
| `date` | `occurred_at`, `date_local`, `source_date` | must be `YYYY-MM-DD HH:MM:SS`; a **fixed** per-ledger UTC offset (default `+08:00`, `--utc-offset`) is applied; no DST, not inferred |
| `money` | `amount` | parsed as `Decimal`; finite, non-negative, ≤2 decimals (never silently rounded); output as 2-decimal string |
| `currency` | `currency` | 3 upper-case letters; no FX conversion |
| `category` | `category` | required; **kept verbatim as the primary category** |
| `remark` | `remark` | kept verbatim |
| `asset` / `from` / `target` | `asset_id` / `from_asset_id` / `to_asset_id` | IDs only (no account names); transfers need both sides |
| `sourceid` | `related_source_key`, `related_record_id` | refund ↔ expense link, not guaranteed complete |
| `hasbx` | `reimbursement_code` | integer or null |

Also rejected: duplicate JSON keys, NaN/Infinity, the same `key` with different content inside one file. Unknown extra fields are kept in the raw comparison document and archive but not interpreted.

**Dedup & revisions** (`qianji/store.py`): same key + same canonical content → no-op (key order, row order and `10` vs `10.0` are not revisions). Changed content blocks the import until you run `import --accept-updates --expected-generation <from preview>`; every accepted revision is kept (`history <id>`). Missing rows are never treated as deletions. Original files are archived byte-for-byte as `data/archive/<sha256>.json`.

**Daily summary** (`qianji-summary/1`, `qianji/processing.py`): grouped by ledger × local date × currency with exact `expense`, `income`, `refund`, `transfer`, `net_expense = expense − refund`, `net_cashflow = income + refund − expense` (not a balance). Transfers are never income/expense; refunds count on their own date.

## 2. Labels / 标签 (`qianji/analysis_core.py`, `qianji/label_rules.py`)

Labels are a separate, optional, derived layer stored in `data/analysis/labels.sqlite3`. Dimensions (`qianji-labels-1`):

| Dimension | Values |
|---|---|
| `item` | 餐食, 咖啡, 茶饮, 饮料, 酒饮, 零食, 水果, 乳品, 日用品, 交通, 医疗, 会员订阅, 水电燃气, 运动, 书籍学习, 数码, 服饰, 居住, 通信, 娱乐 |
| `meal` | 早餐, 午餐, 晚餐, 加餐 |
| `channel` | 外卖, 网购, 线下 |
| `context` | 通勤, 出差, 旅行, 聚餐, 学习, 工作 |
| `record_note` | 平账标记, 代付标记, 报销标记, 退款说明 |
| `merchant` | free text, must be quoted **verbatim** from the remark |

Rule labels (state `automatic`, effective immediately), applied to expenses/refunds:

1. **Category map** — the user's own Qianji category is explicit evidence: e.g. `三餐/早餐/午餐/晚餐/餐饮 → item:餐食`, `奶茶 → 茶饮`, `水电煤 → 水电燃气`, `话费网费/网费/话费 → 通信`, `电器数码 → 数码`, `住房/房租 → 居住` (`CATEGORY_MAP` + `CATEGORY_ALIASES`).
2. **Balance marker** — a remark starting with `平账(` / `平账（` → `record_note:平账标记` (amount/kind untouched).
3. **Exact whole-remark rules** — the *entire* remark, after case-folding and collapsing whitespace (≤240 chars), must equal a phrase: `lunch → meal:午餐 (+item:餐食)`, `midnight meal → meal:加餐`, `coffee/americano → item:咖啡`, `milk tea → item:茶饮`, `toothpaste → item:日用品`, `bus fare → item:交通`, … No substring or fuzzy matching, so `not beer` or `coffee and milk` never match.
4. **Context rules** — ambiguous words need the source category: `(水电煤, water) → 水电燃气` but `(零食, water) → 饮料`; `(交通, subway)`, `(学习, notebook)`, `(话费网费, phone) → 通信` vs `(电器数码, phone) → 数码`.

Model candidates (optional): remarks not fully covered by rules can be sent to a **local Ollama** (`127.0.0.1:11434`, proxies disabled) — only `category` and `remark` are sent, never amount, account IDs or kind words. The model must answer a JSON schema whose tags are an enum of the vocabulary above; every label needs an **exact quote** from the remark (located by the program), merchants must appear verbatim, duplicates/unknown tags/bad quotes are rejected (`invalid_prediction_*`). Accepted outputs are stored as **`proposed`** and never enter effective dimensions or totals until reviewed. Inputs are fingerprinted (`id`, revision, kind/category/remark) together with the model recipe (model digest, prompt, schema, sampling), so unchanged inputs are never re-inferred and a source revision invalidates old labels.

Human review (`python -m qianji.analysis review --file .local/review.json`, then `publish`): `accept`/`reject` decisions per dimension with optimistic concurrency (`expected_revision`) and idempotent `request_id`. Confirmed labels take precedence over rules and models; rejections persist across recipe changes.

After each import the CLI enqueues a background job (`qianji/auto_analysis.py`, queue in `data/analysis/jobs.sqlite3`): publish rule labels first, then process model-eligible remarks; offline model → back-off retry without consuming per-record retry budget. Manual entry points: `process-analysis.cmd` (≤20 remarks) and `process-analysis-all.cmd` / `python -m qianji.batch_analysis` (whole publication, batches of 200, ≤3 rounds). Details: [ANALYSIS.md](ANALYSIS.md).

## 3. Taxonomy v1 / 三级分类 (`qianji/taxonomy_v1.py`)

A frozen tree with stable machine IDs (`food`, `food.meals`, `food.meals.lunch`, …): `food` (meals / drinks / snacks / fruit / tobacco_alcohol), `home` (supplies / utilities / housing), `transport`, `health`, `learning`, `digital`, `personal`, `leisure`, `membership`. Its vocabulary and semantics are hashed into `RECIPE`; changing semantics means adding `taxonomy_v2`, never editing v1.

`classify(row, entry)` produces, for `category_l1..l3` and `channel/context/meal/merchant`, a decision `{value, state, candidates, evidence}` with `state ∈ known | unknown | ambiguous | not_applicable`:

- Only `expense` and `refund` are classified; income and transfer are `not_applicable`.
- Evidence = rule tags (or the processed label entry, which is authoritative even if empty) + confirmed/proposed review labels. **Confirmed beats automatic**; `proposed` only appears as candidates.
- Each tag maps to a node: item labels → level-2 nodes, meal labels → `food.meals.*`, some literal categories → deeper nodes (`房租 → home.housing.rent`, `书籍 → learning.learning.books`, `理发 → personal.haircut`).
- **Remark refinement**: an exact remark detail (e.g. `toothpaste → home.supplies.hygiene`, `subway → transport.transport.subway`, `water` under `水电煤` → `home.utilities.water`) is used only when an effective item tag supports its parent; inside the same L1 domain it replaces the coarser category node.
- **Drink vs meal**: a drink category with a meal remark (`咖啡` + `breakfast`) keeps the drink path and records the meal occasion separately.
- **Conflicts**: if effective evidence points to different nodes at a level, that level and deeper become `ambiguous` with candidates; nothing is guessed, and amounts are never split.
- Clock time never implies a meal; refunds use their own evidence, not the related purchase.

Synthetic examples (run `classify()` yourself):

| category | remark | category_l1 / l2 / l3 | meal |
|---|---|---|---|
| 三餐 | `lunch` | food / food.meals / food.meals.lunch | 午餐 |
| 咖啡 | `breakfast` | food / food.drinks / food.drinks.coffee | 早餐 |
| 日用品 | `toothpaste` | home / home.supplies / home.supplies.hygiene | – |
| 水电煤 | `water` | home / home.utilities / home.utilities.water | – |
| 三餐 | `coffee and a bagel` | food / food.meals / *unknown* | – |
| 交通 | `coffee` | *ambiguous* (food, transport) | – |
| (income) | – | not_applicable | not_applicable |

## 4. Analytics / 分析 (`qianji/analytics.py`, `qianji/analytics_query.py`)

`reprocess` (preview → `--apply --expected-plan <plan_ref>`) builds an immutable analytics publication: **one fact per bill** (all normalized fields + `dimensions` month / ISO week / weekday / category_l1-3 / channel / context / meal / merchant + full `classification` evidence + `source_fingerprint` + `label_basis`). Label entries are reused per record only when the input fingerprint still matches (`label_source_state = matched | partial | absent`).

Queries (`/api/v1/analytics/{snapshot,catalog,facts,cube,quality}` and the MCP tools) always carry the pinned `analytics_ref`. Cubes group by 1–3 dimensions with exact Decimal metrics; `unknown`/`ambiguous` stay null groups (never zero, never redistributed); proposed labels are excluded. `quality` reports per-dimension state counts, expense amount per state, remark coverage, label reuse and refund-link status. Contract: [docs/ANALYTICS.md](docs/ANALYTICS.md).

## 5. Rebuild / 基础重建 (`qianji/rebuild.py`)

`python -m qianji rebuild` regenerates current base data from the archived original referenced by each record's **latest accepted revision**, validates exact totals and writes a field diff — preview only by default; apply with `--apply --expected-plan <plan_ref>`. Identities, revisions and history are preserved; `generation` changes only if content or processing versions change. See [docs/REBUILD.md](docs/REBUILD.md).

## Quick start / 快速开始

```powershell
python -m qianji preview "export.json"     # new / duplicate / revised counts, imports nothing
python -m qianji import  "export.json"     # archive + import + publish data/exports/current.json (+ background labels)
python -m qianji status; python -m qianji verify
python -m qianji reprocess                 # preview analytics; then --apply --expected-plan <plan_ref>
python -m qianji serve                     # read-only API on http://127.0.0.1:8790
```

Optional: `./setup-mcp.ps1` creates `.local/mcp-venv` from `requirements-mcp.lock` for the read-only MCP server ([MCP.md](MCP.md)). Optional label config: copy [`analysis-config.example.json`](analysis-config.example.json) to `data/analysis/config.json`. Full Chinese usage guide: [docs/USAGE.md](docs/USAGE.md); data contract: [DATA_CONTRACT.md](DATA_CONTRACT.md); reader service: [READER_SERVICE.md](READER_SERVICE.md).

## Tests / 测试

```powershell
python -m unittest discover -s tests -v
```

All tests generate synthetic bills in temporary directories (`合成…` / `synthetic …` remarks, small round amounts). MCP tests need the packages in `requirements-mcp.lock`.

## Privacy / 隐私

- No personal data ships with this repo. Everything you import stays under `data/` (archives, SQLite, publications, analysis) and `.local/` on your machine, both Git-ignored; `.gitignore` is an allowlist (`/*` then `!` code/docs only) plus explicit denies for exports, `*.csv/xlsx/db/sqlite*/parquet`, logs and `.env*`.
- Nothing is uploaded. The optional model call goes only to a local Ollama on `127.0.0.1` with category + remark text. HTTP and MCP are read-only and loopback-only.
- Archives are not encrypted; back up `data/` yourself.

隐私：仓库不含任何账单、金额、商家或账户数据；导入的数据只保存在本机被 Git 忽略的 `data/`、`.local/`；不上传任何内容，可选模型只调用本机 Ollama。

## Disclaimer / 声明

钱迹 / Qianji is a third-party app; this project is not affiliated with or endorsed by it. It only processes export files **you** produced from your own account, relies on the current export format (which may change), and gives no financial advice. Classification coverage is not accuracy; model candidates are unverified until you review them.

本项目与钱迹官方无关，仅处理你本人从自己账户导出的文件；导出格式可能变化；分类覆盖不等于准确，模型候选需人工复核。不构成任何财务建议。

## License

[MIT](LICENSE)
