# 学生手册 RAG 学习项目

目前已完成文档解析、表格校对、程序化分块、表格上下文关联和266块向量化，并接入混合检索、重排、带引用的答案生成及CRAG。固定10题基线评测与运行入口见文末“固定10题评测”。

## 当前结果

- **首版分块：`output/chunks-v1/chunks.jsonl`**，由 Python 规则程序从校对版全文自动生成，规则见下文；`chunks.txt` 是同一批结果的纯文本阅读副本。
- **后续索引使用：`output/linked-v1/chunks.jsonl`**，保留相同 266 块正文，增加表格与说明正文的关联，以及带原文介绍的检索文字。
- **后续使用的全文数据：`output/reviewed-v1/document.json`**，244 页，表格已经替换为校对后的 JSON 行记录，正文去除样式标签。
- **单独的表格数据：`output/reviewed-v1/tables.json`**，21 个源表格块整理为 30 个子表、231 条行记录。
- 校对数据及来源记录：`data/table_reviews.json`；重建与检查脚本：`scripts/build_reviewed.py`。
- 校对版运行检查和输出哈希：`output/reviewed-v1/run.json`。
- 输入：`data/input/student-handbook-2025.pdf`，从桌面原文件复制，原文件未修改。
- 原始整本基础版：`output/flash-v1/markdown.md`，覆盖 244 个物理 PDF 页面，仅作为提取基线留档。
- 结构化内容：`output/flash-v1/structured_content.json`。
- 完整中间协议：`output/flash-v1/middle_json.json`。
- 原始模型/原生解析输出：`output/flash-v1/model_output.json`。
- 图片素材：`output/flash-v1/images/`。
- 运行信息与检查：`output/flash-v1/run.json`。
- 单页对照：`output/basic-page37-v1/`（Basic/auto）与 `output/basic-ocr-page37-v1/`（Basic/ocr）。这两版仍存在同一表格行合并问题。

`flash-v1` 使用 Flash/TXT，直接读取 PDF 文本层，不运行 OCR 或视觉语言模型。整本解析、保存及结构检查实测约 11 秒，不包含安装和人工质量检查。

原始提取版仍保留当时的错误。校对版已逐页对照原 PDF 检查全部 21 个已识别表格块，修正或重建其中 5 个块，并展开其余表格的合并单元格、分组和续表关系。第 37 页的绩点映射已拆开，第 128、129、134 页的排版文本已重建成数据，第 133 页的 A/B 类标签顺序已修正。具体范围和边界见 `QUALITY_REVIEW.md`；本轮没有审计可能漏识别的表格，也没有全面校对非表格正文。

## 校对版 JSON 数据格式

`data/table_reviews.json` 是校对内容的编辑入口；两个输出 JSON 是同一份校对数据的自动生成视图。原 PDF 的哈希、原表内容哈希、物理页码、印刷页码和原始内容块索引均保留，避免更新提取基线后误套旧校对。

- `tables.json` 每个表格包含 `title`、`section`、`continuation_group`、`subtables` 和来源信息。
- 子表以 `columns` 指明字段，以 `rows` 保存按字段命名的行记录；复杂原表按不同表头拆分，续表仍保留各自来源页。
- `source_notes` 保存原文备注，`editorial_notes` 保存整理说明，两者分别存放。
- `shared_values` 保存多个项目共用的原表合并单元格。例如六项单项奖共用的 `20%` 不会复制成六条独立比例。
- 数值、正负号、单位和范围保留为原文字符串；`null` 表示原表该位置不存在，不当成数字 0。
- `document.json` 按页保存全文内容；`type=table` 的块内 `content` 就是完整表格数据，其他块的 `content` 是去除样式后的原提取内容。原 PDF 和原始图片仍在原目录，不复制到新 JSON。

新校对数据不存 HTML 表格，不创建网页或可视化。它是本项目的数据格式，不冒充 MinerU 的 MiddleJson；原始 MinerU 文件仍保留其原有格式作为溯源基线。

## 目录

```text
data/input/       原始输入副本
tools/MinerU/     上游源码，不加入本项目 Git
.venv/            Python 运行环境，不加入 Git
.runtime/         本项目模型和运行缓存，不加入 Git
scripts/          可重复运行的安装、提取脚本
output/           提取产物及运行记录
logs/             控制台日志、人工检查用截图，不加入 Git
```

## 程序化分块 v1

入口是 `scripts/chunk_document.py`，参数在 `config/chunking-v1.json`。程序只使用 Python 标准库，不联网、不加载模型，不让大模型逐块生成、改写或概括原文。以前聊天中的示例是讨论时人工选择的示意，本目录中的结果才是规则程序的实际输出。

规则固定如下：

1. 包含 `index` 块的页面作为目录页整体排除；图片、页码块排除。其他文字保留，包括前言和签名页，不用模型判断“是否有用”。标题识别文档标题、附件编号、附件之后的短标题，以及不超过 45 字符的“第…章”“第…节”“一、…”格式；普通正文即使被提取器标成 `paragraph_title` 也不会因此变成分块边界。同页紧邻文档标题的短标题前缀合并；文档开头的独立发文编号并入标题上下文。
2. 同一标题范围内先拼接正文；跨页不自动分块，跨行断词直接衔接。条款或子项开头、句末和短标签之间补换行，清除字符串首尾空白，不改写原句和数字。
3. 以“第…条”为完整单元，在本节内按顺序装入每块最多 **1000 字符正文**。不跨文档、章、节、附件和表格边界强行合并。字符数不是模型 token 数；文档和章节标题上下文另计，所以最终 `text` 可以略长于 1000 字符。
4. 单条超过上限时，优先按完整子项切，再按完整句子切；仍无边界才按字符截断。长条款若有不超过 200 字符的列表引导句，后续块重复它；否则重复条款编号。普通正文不做固定窗口重叠。
5. 表格按子表和完整行打包，字段采用“列名：原值”，保留共同字段和原文备注并在每组重复。不会把一行拆开，也不会把整理说明 `editorial_notes` 当作制度内容。原表空值写为“（原表未列值）”，不会转为零。若一行加共同说明本身超限，允许整行超限并单独记录；当前没有这种例外。
6. 每个最终块只有 `chunk_id`、`text`、`source_file`、`pdf_pages` 四个字段。`text` 带必要的制度和章节名称，便于区分同名条款；不保存 `bbox`、版面 `type`、原解析树等。页码是正文证据所在的物理 PDF 页码，标题来自更早页面时不会把早期页码当成本块证据页。

当前生成 **266 块，其中表格 31 块、231 行**。含上下文标题的 `text` 长度最小 53、中位数 460、最大 1067 字符。较短的独立章节保留短块；首版没有为追求长度一致而跨章合并。实际第 82 页“第三章 认定程序”将第十条、第十一条及三个办理步骤放在同一块。

`audit.json` 单独保存块到输入字符区间、标题和表格行的映射，供检查；不是送入 Embedding 的内容。程序校验每个保留正文块的字符区间连续且只覆盖一次，标题均被使用、全部表格行只输出一次。重复的标题、表格共用说明和长条款引导句属于有意保留的上下文。`run.json` 保存规则、输入/脚本/输出 SHA-256 与统计；相同输入及规则的输出可逐字复现，`--check` 会重新计算并逐个比较全部文件。

覆盖校验只证明现有输入数据没有在分块时遗漏，不证明原始 PDF 提取本身全部正确，也不证明每一块都是最优检索单元。首版的表格前后短句可能各自成块；这些分块在后续关联步骤中建立连接，保留分块正文。后续用真实问题检查召回效果再调整规则，无须逐块人工重写。当前不是索引或检索效果验收。

## 表格与正文关联 v1

入口是 `scripts/link_context.py`，参数在 `config/table-context-v1.json`。本地 Python 扫描已有 JSON 结构，不重新解析 PDF、不调用模型、不生成摘要。无需让大模型读取或逐段处理全文。已有 `chunks-v1` 保持原样；关联版另存为 `output/linked-v1/`。

- 使用表格登记的 `continuation_group` 连接同一制度中的表格和续表，形成 13 组，覆盖全部 31 个表格块。原表来源 ID 保存在独立的 `relations.json`。
- 表格前后最近的正文块，只有在标题来源完全一致、页距不超过 1 页时才连接；不跨文档、章或附件仅凭相邻顺序连接。
- 附件编号只能匹配同一制度中的附件目标。附件表还可以连接到同一制度中名称与附件标题前缀精确匹配的章节，例如“第二章 德育评价”对应“德育评价日常记实考评指标”；不使用模糊关键词或模型判断。
- 关联是双向的：命中表格可以补正文，命中正文可以补表格及同组的补充说明。同一具体表格组内的成员可直接关联，不沿新补入块继续扩展到另一组。
- 新增 `context_group_ids`、`related_chunk_ids`，并提供 `retrieval_text`。后者在表格文字前加最多 200 字符的完整原文片段，作为未来 Embedding/重排的输入；既有 `text`、页码和源文件不改。最终回答上下文使用 `text`，关联图不传给回答模型。

当前 13 组均匹配到正文，51 个块有直接关联。另记录 8 处无匹配表格目标的附件引用（主要是其他制度的申请表附件）；保存在 `relations.json` 的 `unresolved_references`，不会误接到另一个制度的同号附件。这里是规则匹配的结果，不表示经过逐组人工语义审定。

已实现独立的 `scripts/context_expansion.py::expand_context`，供将来的检索流程在召回、重排后调用。只检查原始命中块的直接关联，**不递归读取新补入块的关联**。参数同时受代码上限约束：最多 8 个原始命中、最多补 4 块、所有入选原文合计最多 6000 字符、最多检查 64 条直接关系；配置只能调小，不能突破上限。这是字符预算，不是模型 token 预算。多命中去重、原始命中优先、补充块按轮次从各原始命中取出。

超限时跳过整块，不截断表格行或数字。返回 `complete=false`、`omitted_chunk_ids` 和 `relations_not_examined`，调用方必须据此保留“上下文未补齐”的状态，不能把受限结果当作完整规则回答。发展评价附件有较多续表，已验证此限制生效。运行预览只输出一次补读结果，没有接入向量数据库或生成模型。

`run.json` 记录输入、配置、关联脚本、补读脚本和产物哈希；`--check` 在本机重算比较，确认生成关系可复现。规则回归覆盖同章相邻、跨章拒绝、附件匹配、跨制度同号附件拒绝、双向关系、续表补读上限和原始正文不变。

## 运行

在本项目根目录的 PowerShell 中执行：

```powershell
# 首次安装或依据锁定依赖恢复运行环境（需要 Python 3.11 和 Git）
.\scripts\setup.ps1

# 整本基础提取；现有输出不会被覆盖，每次使用新的名称
.\scripts\extract.ps1 -Tier flash -OutputName flash-v2

# 单页 Basic 版面/表格模型实验；首次按需下载模型
.\scripts\extract.ps1 -Tier basic -Pages 37 -OutputName basic-page37-v2

# 对同一页强制 OCR，区分文本层和图像识别的效果
.\scripts\extract.ps1 -Tier basic -OcrMode ocr -Pages 37 -OutputName basic-ocr-page37-v2

# 根据校对 JSON 重建全文和表格数据（不调用模型、不联网）
.\.venv\Scripts\python.exe .\scripts\build_reviewed.py

# 只读检查输出是否与当前校对数据一致
.\.venv\Scripts\python.exe .\scripts\build_reviewed.py --check

# 按固定规则生成分块；更改规则后在这里重新生成
.\.venv\Scripts\python.exe .\scripts\chunk_document.py

# 重新计算并比对分块、溯源映射与运行记录
.\.venv\Scripts\python.exe .\scripts\chunk_document.py --check

# 建立表格与正文关联，不调用模型
.\.venv\Scripts\python.exe .\scripts\link_context.py
.\.venv\Scripts\python.exe .\scripts\link_context.py --check

# 只读预览：命中图书借阅表后，按硬上限补入说明块
.\.venv\Scripts\python.exe .\scripts\link_context.py --expand chunk-0225

# 检查已对照原图的关键数值与合并单元格约束
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

执行策略限制 `.ps1` 时，可以只对本次进程运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\extract.ps1 -OutputName flash-v2
```

脚本通过 MinerU 公开 Python SDK 的 `parse()` 和 `ParseResult.save()` 生成原生结果，不使用大模型改写手册内容。Flash 使用 `ocr_mode=txt`；Basic/Standard 使用 `ocr_mode=auto`。本次没有配置云端文档解析、远程 VLM 或 LLM 后处理。Basic 的模型文件由 ModelScope 下载到本项目 `.runtime/mineru/models/`，解析在本机进行。标准模式还需要额外的 VLM 权重，尚未安装和验证。

## 自行运行 Embedding

`scripts/embed_chunks.py` 使用 Python 标准库发送 HTTPS 请求，不需要额外安装 SDK。2026-10-20 使用用户提供的北京业务空间地址，进行了两次同一短句的连通测试（一次直接请求、一次经正式客户端），每次均返回 `qwen3.7-text-embedding` 的 1024 维向量，各报告 21 Token，合计 42 Token。本轮未发送手册分块，未执行整本向量化。正式客户端的最近一次连通检查约 0.25 秒，单次观测不代表批量吞吐。

用户提供的 API Key 和 Base URL 已存入 `.runtime/dashscope.local.json`，仅在本地保存且被 Git 忽略；程序和运行记录均不写出密钥。环境变量 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL` 可分别覆盖本地设置。迁移机器时自行配置这两项，不能仅复制仓库后期待密钥也在其中。

在 PowerShell 执行：

```powershell
Set-Location 'D:\all_try\codex_try\student-handbook-rag'

# 可选：仅检查输入和批次数，不联网、不写输出
.\.venv\Scripts\python.exe .\scripts\embed_chunks.py --dry-run

# 正式执行：从头开始，或者从上次成功保存的位置继续
.\.venv\Scripts\python.exe .\scripts\embed_chunks.py

# 完成后只读校验全部批次和汇总文件，不调用 API
.\.venv\Scripts\python.exe .\scripts\embed_chunks.py --check
```

也可以先运行 `embed_chunks.py --limit 2`，本次最多处理 2 个新块，然后执行不带 `--limit` 的同一命令完成其余块。用户自行启动的执行才会发送这些手册数据。输入固定为 `output/linked-v1/chunks.jsonl` 的 `retrieval_text`，按文件顺序串行请求，每批 10 个块；266 块的首次完整执行为 27 批。不把全部文字拼成一条，不做改写，不将表格关联补读或问答生成混入向量化步骤。

默认输出目录为 `output/embeddings-v1/`，仅在用户执行后生成整本向量文件：

- `manifest.json`：输入文件哈希、服务地址、模型、维度、字段和批大小，续跑时必须匹配。
- `batches/000000.json`、`000010.json` 等：按本批首个块的零基位置命名，包含向量、块 ID、位置、输入文字哈希、本批用量和内容校验和。若先小批试跑，文件名可能是 `000000.json`、`000002.json`，仍连续衔接。
- `run.json`：总块数、已完成块数、`partial/completed` 状态，以及已保存批次的 Token 用量。
- `vectors.jsonl`：仅在全部完成后，合并为与输入相同顺序的向量数据，一行一个分块，`embedding` 为数字列表；没有重复存正文，使用 `chunk_id` 对接原分块和表格关联。
- `run.lock`：系统文件锁，阻止两个进程同时写入同一目录；退出或崩溃后由系统释放，不需人工删除锁文件。

每批返回后按 `index` 还原输入顺序，校验数量、模型、维度、数值有限且非零，再通过临时文件与原子替换保存。成功落盘的批次不会重发；Ctrl+C 后执行同一命令续跑。若响应已到但落盘前进程退出，未保存的那一批仍可能需要重发并重复计费；超时和失败请求也可能产生服务端用量，运行记录不能替代实际账单。

参数在 `config/embedding-v1.json`。默认 1024 维、每批 10 条，超时 30 秒，瞬时网络错误、429 和部分 5xx 最多尝试 3 次，401/403 等直接停止。每次运行最多 90 次 HTTP 尝试；输入还受 1000 块、每块 2000 字符、总量 20 万字符的代码上限约束，超限在发送前报错。这些是请求与输入上限，不是账户人民币消费硬限额，也没有查询账户余额。API 格式和模型参数来源：[百炼向量同步接口](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)。

如果输入、服务地址、模型、维度或批大小变化，原目录会拒绝继续混写。可以用 `--output output/embeddings-v2` 另存一版；这会重新请求新目录中尚无的全部向量。未来用户问题必须采用与文档相同的模型与维度，按模型推荐的查询编码方式生成查询向量。生成的向量目录被 Git 忽略，避免大量浮点数据进入代码提交；原分块和可复现规则仍受版本控制。

## 实际问题检索：向量 10 块 + 关键词 1 块，去重后重排返回 5 块

2026-10-20 检查用户运行的 `output/embeddings-v1/`：状态为 `completed`，266 个 1024 维向量、27 批，已保存批次报告用量 82,848 Token。批次校验和、输入哈希、维度、块 ID 顺序及汇总向量文件均通过只读检查，不重新计算文档向量。

入口 `scripts/retrieve_chunks.py`。直接在 VS Code 使用项目解释器运行此文件，会提示输入一个问题；或者在项目目录终端执行：

```powershell
.\.venv\Scripts\python.exe .\scripts\retrieve_chunks.py --question "本科生在图书馆最多能借几本书，借期多少天，可以续借多久？" --top-k 10

# 默认先召回 10 块，再重排返回前 5 块
.\.venv\Scripts\python.exe .\scripts\retrieve_chunks.py --question "申请国家奖学金，需要满足哪些条件？"

# 可选：为重排后的第一名补上已关联正文/表格，仍受原有 4 块/6000 字符限制
.\.venv\Scripts\python.exe .\scripts\retrieve_chunks.py --question "百分制成绩95到99分，对应的绩点是多少？" --top-k 10 --expand-top 1

# 相同问题可强制只使用本地查询和重排缓存，任一步没有缓存则报错
.\.venv\Scripts\python.exe .\scripts\retrieve_chunks.py --question "百分制成绩95到99分，对应的绩点是多少？" --offline

# 对照实验：明确关闭重排，恢复原来的余弦 Top-10
.\.venv\Scripts\python.exe .\scripts\retrieve_chunks.py --question "申请国家奖学金，需要满足哪些条件？" --no-rerank --no-keyword
```

实现流程：

1. 检查本地向量与源分块和配置是否一致，一次读取后归一化为单位向量。已有文档向量不发送给 API。
2. 每个新问题调用同一 `qwen3.7-text-embedding` 模型，返回 1024 维查询向量。目前采用与既有兼容接口相同的纯文本编码，没有添加任务指令，也未启用 DashScope 原生 `text_type=query`。这种配置是可运行的首版基线，不代表已经完成查询编码方式的比较。
3. 计算 `cosine(q,d) = dot(q,d) / (norm(q)*norm(d))`；归一化后就是点积。精确扫描全部 266 个向量，按得分降序取前 10，相同得分按源分块顺序排列。小数据集无需近似索引；相似度取值在 -1 到 1 之间，不是正确率或答案置信度。
4. 查询向量按语料身份、模型、维度、编码版本和完整问题文字缓存到 `.runtime/query-embeddings-v1/`；同一问题重复使用缓存，报告 `cache_hit=true`、本次 API 尝试次数为 0。不同问法不自动共用缓存。
5. 在向量前 10 块之外，使用本地 BM25（中文二元词组、英文和数字词）只取关键词第 1 名；按块 ID 或去除空白后的正文去重。若它重复或无匹配，不补第二名。因此重排输入是 10 或 11 块。默认把合并候选的 `retrieval_text` 与问题发送给 `qwen3.7-text-rerank`，按相关性返回前 5 块。不会发送整本正文或重做文档向量。表格的检索文本保留此前关联的简短说明，最终命中仍返回原始 `text`、块 ID 和页码。相同重排分数沿用余弦排名。
6. `hits` 是最终 5 块，包含 `rank`、`rerank_score`、`cosine_rank` 和 `cosine_similarity`；`vector_candidates` 留存初次召回的 10 块，`keyword_retrieval` 记录唯一关键词候选及去重结果，`rerank_candidates` 记录合并候选，`reranking.candidate_scores` 留存全部候选重排得分用于对照。两种分数不可直接比较，也不是回答正确概率。固定返回前 5，不做分数阈值过滤，不承诺五块都能用作某项政策的依据。
7. 重排缓存位于 `.runtime/reranking-v1/`，依据语料身份、服务地址、模型、任务指令、完整问题、候选 ID 顺序及每块输入文本哈希定位，并校验缓存内容。相同输入复用缓存，本次 HTTP 次数和计费用量均为零；候选、配置或文字改变则重新请求。`--offline` 要求查询向量和重排两份缓存都存在。
8. 默认不扩展关联块；`--expand-top N` 可补读最终重排结果的前 N 块（默认结果最多 5），扩展结果另存在 `answer_context`，不改变五块 `hits`。补读仍受原有硬上限限制。`--no-rerank --no-keyword` 可显式复现余弦基线；重排请求失败会停止，不会悄悄返回余弦结果冒充重排结果。

每次最多 8 个问题、每题 500 字符、`--top-k` 表示初次召回候选数（默认 10，开启关键词补充时最大 19，关闭后最大 20）；查询 Embedding 和重排各最多 24 次 HTTP 尝试。重排配置在 `config/reranking-v1.json`，默认 `top_n=5`；候选少于五块时只能返回实际数量。重排每块最多 2000 字符，超时 30 秒，瞬时错误最多尝试三次，401/403 等不重试。沿用本地同一 Key 和北京业务空间，由兼容 Base URL 自动构造 DashScope 原生重排地址，不额外安装 SDK。实现为 `scripts/rerank_api.py`（请求与校验）、`scripts/rerank_chunks.py`（缓存、索引映射与选择）。接口依据：[百炼文本排序 API](https://help.aliyun.com/zh/model-studio/text-rerank-api)。

检索入口本身不调用生成模型。控制台只显示最终排名、得分、原召回名次、块 ID、物理页码和标题；完整原文在 `output/retrieval-v1/last_run.json`（schema_version=2），该目录被 Git 忽略。API 为全部候选计量，所以请求全部重排分数后在本地选择五块，便于校验每个原始索引与留档，不增加候选输入量。用量记录只包含已收到的成功响应，失败或超时可能仍计费。

实际两题初次测试结果（不是评测集结论）：

| 问题 | Top-1 | 余弦得分 | PDF 页码 | 命中内容 |
| --- | --- | --- | --- | --- |
| 本科生在图书馆最多能借几本书，借期多少天，可以续借多久？ | chunk-0225 | 0.800901 | 193 | 本专科生、继续教育生：20 册，借期 30 天，续借 15 天 |
| 百分制成绩95到99分，对应的绩点是多少？ | chunk-0024 | 0.710141 | 37 | 百分制 95～99，对应绩点 4.5 |

上述为接入重排前的基线：两次查询 API 各报告 35、31 Token，合计 66 Token。已使用 `--expand-top 1` 验证表格可补齐前后说明；完整 Top-10、补读结果和用量保存在本机 `output/retrieval-v1/smoke_test.json`。如今复现该基线须加 `--no-rerank`。余弦算法参考：[百炼语义搜索示例](https://help.aliyun.com/zh/model-studio/embedding)。

2026-10-20 重排实际验证：

| 问题 | 重排后的五个块（依次） | 重排报告 Token |
| --- | --- | --- |
| 申请国家奖学金，需要满足哪些条件？ | 0157、0156、0162、0146、0171 | 2272 |
| 申请国家励志奖学金，需要满足哪些条件？ | 0162、0146、0156、0157、0160 | 2205 |

编号均带 `chunk-` 前缀。国家奖学金的成绩要求块 0157（0.963081）和国籍、年级等条件块 0156（0.939879）均进入前二；励志奖学金的主要申请条件块 0162（0.958078）仍为第一。两次重排合计 4477 Token；按当日北京原价 0.5 元/百万输入 Token 算，约 0.00224 元，实际扣费取决于免费额度等账户情况（[官方价格](https://help.aliyun.com/zh/model-studio/model-pricing)）。已有查询向量缓存，本次没有新的查询 Embedding 用量。结果在本机 `output/retrieval-v1/rerank_smoke_test.json`；离线重复验证相同排序且两阶段均无 HTTP 请求。

两题的前五仍混入其他奖项，说明重排没有彻底排除相近政策；当前按要求固定返回五块，不自行加入政策过滤。后续生成回答时必须分辨适用奖项与条款，不能把五块中的所有条件拼起来当成同一政策。此结果仅为功能验证，不是准确率评测。

本次实测文件逻辑大小：源码约 48 MiB，运行环境约 561 MiB，按需下载模型约 248 MiB，正式输出约 14 MiB。连同原 PDF、运行缓存和保留的两份重试临时输出，项目约 900 MiB（约 0.88 GiB；不含系统包下载缓存与文件系统分配开销）。模型整包约 818 MiB，但本次只下载实际用到的文件，没有下载整包或 VLM。两份重试输出位于 `.runtime/incomplete-flash-v1*`，不加入 Git；清理请求被自动审批策略阻止，故保留原状。

`page_idx` 是从 0 开始的物理 PDF 页序；用户看到的物理 PDF 页码为 `page_idx + 1`。正文中印刷的页码是另一套编号，不使用固定偏移作为通用规则。分块以校对版 `reviewed-v1/document.json` 为输入；后续建立索引使用 `linked-v1/chunks.jsonl` 的 `retrieval_text`，回答时按命中 ID 经补读函数取原文，不要继续把原提取版含错的表格用于问答。

## 完整问答：检索依据后生成答案

入口 `scripts/answer_question.py`，在 VS Code 选择项目 `.venv` 解释器直接运行，会提示输入一个问题；命令行用法：

```powershell
.\.venv\Scripts\python.exe .\scripts\answer_question.py --question "申请国家奖学金，需要满足哪些条件？"

# 查询向量和重排必须有缓存；生成仍发送一次 API 请求
.\.venv\Scripts\python.exe .\scripts\answer_question.py --question "申请国家奖学金，需要满足哪些条件？" --retrieval-offline

# 完全不调用 API：要求已有检索缓存，只准备并保存提示词
.\.venv\Scripts\python.exe .\scripts\answer_question.py --question "申请国家奖学金，需要满足哪些条件？" --dry-run
```

此入口复用余弦召回 10 块、关键词只补 1 块、去重并重排保留 5 块的实现，再以这五块为种子调用既有直接关系补读。仍最多补 4 块、总原文最多 6000 字符，不截断表格行；遗漏状态传入提示词。`complete` 仅表示已知关联的打包覆盖，不保证检索或答案语义完整。来源去重及预算由原补读函数处理，不递归扫描整本。

`config/answer-system-v1.txt` 是可直接修改的系统提示词，要求依证据回答、区分相似政策、保留数字和例外、资料不足时明确说明，并把资料里的指令当作参考文字。关键结论需给出制度名称、已有条款及支持结论的关键原文，再标注 `[资料N]`，不能只给编号；未出现的条号不得编造。无关资料直接忽略，不在答案末尾解释排除了哪些资料。终端末尾的“引用来源”只展示回答实际引用的资料名称和物理 PDF 页码。用户消息用 JSON 明确填写问题、补读状态及来源列表；每条来源包含编号、原文、分块 ID、文件和物理 PDF 页码。不把向量、相似度、解析器版面字段传入 LLM。

`config/generation-v1.json` 配置 `qwen3.7-plus`、`enable_thinking=false`、温度 0.1、最多 2500 输出 Token、超时 60 秒，总提示词最多 16000 字符。`scripts/generation_api.py` 沿用现有 Key 和兼容 Base URL，向 `/chat/completions` 发送一次非流式请求；不依赖额外 SDK。每个生成阶段最多一个 HTTP 尝试，不自动重试、续写或切换模型；新版流程另有证据评估、草稿复核及至多一次改写，见后文；达到输出上限保留文本但标记 `truncated`，失败显式停止。接口依据：[百炼 Chat Completions](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)。

发送前将准确提示词与来源保存到 `output/answers-v1/last_run.json`，成功后补入未经改写的回答、实际模型、用量、完成状态和引用编号检查。失败保留 `generation_failed` 状态及尝试次数。目录被 Git 忽略；不保存密钥。生成答案暂不缓存，重新执行通常会再请求一次 LLM；只有查询向量和重排自动复用缓存。引用检查只核对 `[资料N]` 编号是否存在，不自动证明每项结论都有对应依据。

2026-10-20 首次实际问题为“申请国家奖学金，需要满足哪些条件？”。查询和重排均从缓存读取，生成恰好一次，报告输入 1943、输出 682、总计 2625 Token，正常结束未截断。实际送入 5 块、2173 原文字符，无新增关联块；回答引用资料 1（0157，PDF 147–148 页）与资料 2（0156，PDF 146–147 页），整合了国籍年级与成绩要求，并明确未采用混入的其他奖项条件。对照这两块原文抽查了 10%/30%、无不及格、审核盖章与突出表现标准。此为单例功能验证，不代表答案完整性或准确率已评测。

## 初版：单条关键词补充与简化 CRAG（2026-10-21）

初版流程（`--legacy-crag`保留）：向量 Top-10 + BM25 Top-1 → 去重 → 对最多 11 个候选重排 → 保留 Top-5 → 按已有关系补读 → 评估依据 → 必要时联网补一块 → 生成答案。关键词命中与向量结果重复时不再找第二条；不使用 RRF，不改动已有分块和文档向量。首次运行可能因候选集合变化重新请求一次重排。

`scripts/keyword_retrieval.py` 是内存中的小型 BM25 索引，不增加第三方依赖或数据库。中文使用重叠二元词组，英文与数字使用整词。BM25 与向量分数不直接相加，合并候选交给同一重排模型判断。

`scripts/crag.py` 是借鉴 CRAG 的简化实现，不是复现论文的训练评估器或知识精炼流程。用 `qwen3.7-plus` 单独进行一次 JSON 证据充分性评估：

- `sufficient`：资料足以回答，跳过联网，直接生成。
- `partial`：保留本地资料，尝试联网补充；仍缺失的部分必须说明。
- `insufficient`：不把不相关本地命中当依据，改用联网补充。若也没有合格补充，直接说明无法确定，不再调用回答模型。

评估不是重排分数阈值，也不是正确率保证。评估请求失败、JSON 非法、引用未知块或输出截断时按不确定处理；联网未补到依据时保守拒答。配置在 `config/crag-v1.json`，评估提示词在 `config/crag-evaluator-v1.txt`。LLM 每次只读取已经选出的有限上下文，不读取整本手册。

`scripts/web_search.py` 使用同一 Key 和北京业务空间，调用原生 `qwen-plus` 的联网搜索，开启 `enable_search`、`forced_search`、`enable_source` 和引用标记。使用该原生接口是为了获取结构化网页标题和真实返回链接；最终回答和评估仍用 `qwen3.7-plus`。接口说明：[百炼联网搜索](https://help.aliyun.com/zh/model-studio/web-search)。

每次最多一个联网 HTTP 尝试，不自动重试。提示限定学校及教育部、财政部官网，代码仅采纳这些域名及其子域名的实际返回 URL，并只保留引用均可对应到合格来源的段落；无引用、混入其他来源、超出预算的段落丢弃。URL 和重复段落去重，最多三个来源，合并成一块最多 2400 字符的临时检索摘要，不写回手册、分块或向量索引。它是模型整理的摘要，不冒充网页原文。官方域名过滤不等于事实核验，模型仍可能错误整理日期或细节；这部分需要真实问题评测。回答区分手册年份与网页资料适用时间。

本地资料与这一个网页块合计仍最多 6000 原文字符；不足时按完整块舍弃本地资料，报告 `omitted_local_ids`，不截断表格。完整运行记录 schema_version=2，保留初始本地上下文、评估决定、联网状态与来源、最终提示词、各步 Token 用量及 HTTP 尝试次数。`provider_search_count` 是服务返回的搜索标记，不是已完成搜索数或账单对账依据。

```powershell
# 正常入口：默认开启单条关键词补充与 CRAG
.\.venv\Scripts\python.exe .\scripts\answer_question.py

# 评估依据，但禁止联网；不足时有限回答或拒答
.\.venv\Scripts\python.exe .\scripts\answer_question.py --no-web

# 对照：关闭 CRAG（仍保留关键词补充和重排）
.\.venv\Scripts\python.exe .\scripts\answer_question.py --no-crag
```

`--dry-run` 跳过评估、联网和生成，完全不调用 API，但要求当前候选对应的检索缓存已存在。`--retrieval-offline` 仅限制检索；评估、必要的联网、生成及新版草稿复核仍可能调用 API。相比原流程，每个问题通常多一次评估请求，资料不足时最多再多一次联网请求；回答模型仍最多请求一次。额外步骤也会产生用量，当前未查询账户余额或建立人民币硬限额。

实测“我不是中国人，我可以拿国家奖学金吗”：评估 `sufficient`，零联网请求，答案引用第七条及 PDF 146、147 页。实测“学校在2026年是否更名为绍兴大学？请给出官方依据。”：关键词新增 1 块，11 个候选重排后返回 5 块；评估 `insufficient`，执行一次联网 HTTP 请求，最终只使用一块带教育部链接的摘要生成答案。也观察到联网结果混入第三方引用时保守拒答的行为，随后收紧了搜索提示。此为分支功能检查，不是准确率测量。

## 版本与许可

- 上游：[opendatalab/MinerU](https://github.com/opendatalab/MinerU)。
- MinerU 4.0.12，源码固定在 `b7f3dd0c5f6e6d66a298239ffd02b328b873918e`。
- DocVortex 0.5.16，Python 3.11.4，具体依赖锁定于 `requirements.lock.txt`。
- 本次个人本地学习用途无需购买 MinerU API 额度或商业许可，运行消耗自己的计算资源。上游当前许可证是 Apache 2.0 加附加条款，在线服务标识要求和达到商业门槛后的许可要求以 [LICENSE.md](https://github.com/opendatalab/MinerU/blob/b7f3dd0c5f6e6d66a298239ffd02b328b873918e/LICENSE.md) 为准；模型依其各自许可证使用。

## 后续学习顺序

当前已完成 Embedding、混合召回、模型重排、简化 CRAG、依据约束的答案生成，以及下述固定10题评测。后续针对评测中的澄清和拒答行为改进，不扩充题库。调整分块规则时须重建证据映射、验证字符及表格覆盖，并生成对应向量。当前没有接入外部向量数据库。

## 固定10题评测

数据及评分约定位于 `data/evaluation/sample-v1/`。固定8道可回答题、1道手册无依据题、1道需要追问题；只使用这10题，没有独立保留测试集。

```powershell
# 当前基线：向量10＋关键词1（去重不补位），重排5，关联补全；关闭CRAG及联网
.\.venv\Scripts\python.exe .\scripts\evaluate_questions.py

# 离线计分；检索程序评分，回答使用助手逐项核对的复核记录，不调用裁判模型
.\.venv\Scripts\python.exe .\scripts\score_evaluation.py --reviews .\data\evaluation\sample-v1\baseline_answer_reviews.json
```

每题原始结果、完整提示词、来源、缓存和用量记录位于 `output/evaluation-v1/records/`；评分结果为该目录的 `report.json` 和 `report.txt`。运行前检查题目、语料及规则哈希；完成结果直接复用，失败时保留已完成的检索。相同输出目录拒绝混用配置、程序或语料版本；需要新一轮回答时另指定 `--output output/evaluation-v2`。输入只有问题及真实检索资料，参考答案和评分要点不发送给回答模型。

2026-10-22首轮：10/10完成，无请求失败；8题在向量、合并、重排和最终上下文阶段的证据组宏平均召回率均为100%，MRR@5为1。8题回答内容均100分，引用覆盖均通过；q009为75分，拒答正确但未指出查对应假期通知；q010为25分，列出条件却未先追问奖种，并存在对年级资格的过度概括。边界行为完整通过0/2，不能描述为两题全部内容错误。

q002还命中了原标注之外、内容等价的手册附录条款，复核后记为有效依据，补充过程保存在 `baseline_answer_reviews.json`。内容评分由助手依据冻结规则复核，并非独立人工盲评或Ragas自动评分。这10题的结果不能外推为整体正确率。

### 对照实验：只保留关键词Top-1

```powershell
# 关闭向量召回；保留原BM25第一名、重排、关联补读和同一生成配置
.\.venv\Scripts\python.exe .\scripts\evaluate_questions.py --retrieval-mode keyword_only --output output/evaluation-keyword-v1
.\.venv\Scripts\python.exe .\scripts\score_evaluation.py --run output/evaluation-keyword-v1 --reviews data/evaluation/sample-v1/keyword_answer_reviews.json
.\.venv\Scripts\python.exe .\scripts\compare_evaluations.py
```

2026-10-22实测10/10完成：8题内容平均分100→95，内容和引用完整通过8/8→7/8；重排后证据组召回100%→89.58%，补读后100%→95.83%。q008只找到了资格条款，漏掉申请程序，两项要点缺失，100→60；q007的续借正文通过既有关联补读补齐，仍100。两道边界题仍为75、25。MRR@5均为1，说明首条相关不等于依据完整。

对照报告：`output/evaluation-keyword-v1/comparison.txt`（另有JSON）；详细答案和扣分记录：同目录`report.txt`；固定复核记录：`data/evaluation/sample-v1/keyword_answer_reviews.json`。原题目、评分规则、手册和索引均未修改。关键词组没有查询向量API调用，实际10次重排、10次生成；单候选重排不能找回缺失资料。

候选由最多11条减少到1条，因此这是移除向量路线的消融，不是相同Top-K预算的算法比较；未更换或刻意降低生成模型。每组只生成一次，未估计生成波动。重新生成须使用新输出目录，并针对新回答重新复核，旧复核文件不能套用。增加实验模式后程序哈希变化，原基线目录保留历史结果；要重新跑混合组须另选目录，例如`--output output/evaluation-hybrid-v2`。

## CRAG独立评估、自适应路由与草稿复核

现在`answer_question.py`默认执行：混合召回与重排 → 关联补读 → 证据评估及路由 → 生成草稿 → 依据复核 → 必要时修改一次。结构化协议在`evidence_assessment.py`，动作执行在`adaptive_routing.py`，草稿复核在`answer_reflection.py`。原`crag.py`保留兼容入口。

新版提示词`config/crag-evaluator-v2.txt`把证据充分性与问题明确程度分开：充分则本地回答；手册可能缺一块则本地补查；需要新年份事实则查官方来源；用户缺少奖种等必要信息则追问；无可靠依据则说明无法确定。`complete`只反映已知关系补读完成，不能代替语义完整性判断。

最多一轮本地补查、补查后一次重新评估、一次联网。补查复用原向量10＋关键词1及重排，补查与初始资料去重后总计最多12块、6000字符，完整块取舍不截断。补查后仍要求本地检索时不循环，依据部分有效则有限回答，否则拒答；评估协议非法或请求失败时暂停回答。联网仍只接受已有官方域名过滤器返回的一块摘要，域名过滤和模型复核都不等于事实保证。

`config/answer-reflection-v1.txt`检查实际草稿的数字、条件、适用对象、漏答和引用。最多一次检查、一次改写，保存草稿、检查输入输出及修订文本；未知引用编号会阻止发布。修改后状态为`revised_not_rechecked`，明确未做第二轮语义复核；检查失败或修订不完整则返回无法可靠作答。追问、拒答等没有事实草稿的终止路径不调用复核器。这是普通LLM草稿复核，不是Self-RAG论文的训练与反思Token实现。

```powershell
# 默认开启新版路由和草稿复核
.\.venv\Scripts\python.exe .\scripts\answer_question.py
# 不联网；仍可补查本地、追问、复核
.\.venv\Scripts\python.exe .\scripts\answer_question.py --no-web
# 对照原版CRAG；或单独关闭草稿复核
.\.venv\Scripts\python.exe .\scripts\answer_question.py --legacy-crag --no-reflection
# 完全关闭证据评估、路由和草稿复核，保留原检索生成
.\.venv\Scripts\python.exe .\scripts\answer_question.py --no-crag --no-reflection
# 纯准备，无API请求，要求检索缓存已存在
.\.venv\Scripts\python.exe .\scripts\answer_question.py --dry-run
```

正常一次问答通常增加证据评估和草稿复核两个请求；复杂路径最多2次评估、1次草稿生成、1次复核、1次改写，另有初始检索、最多一轮补查及一次联网的请求。每个生成客户端仍只尝试一次。`--retrieval-offline`也约束补查检索；新补查没有缓存时停止，不暗中联网。schema_version=3记录每个评估、补查、路由、草稿和复核结果；终端仅显示最终答复及简要状态。

独立评估复用原10题的混合、关键词两组上下文，共20份。标签在`data/evaluation/sample-v1/crag_cases.json`，在调用前建立，不传给模型；模型只接收问题与原文。18份参与充分性评分，2份模糊奖学金问题按澄清动作评分。该评估只看证据判断和推荐动作，不执行补查、联网、回答或反思。

```powershell
# 当前修正版20份独立判断；相同版本完成记录会复用，不重复收费
.\.venv\Scripts\python.exe .\scripts\evaluate_crag.py --version adaptive --output output/evaluation-crag-adaptive-v2
# 修改提示词或代码后需另选输出目录，例如output/evaluation-crag-adaptive-v3
# 两个已有问题的补查/复核演示；执行过的目录会拒绝重复调用
.\.venv\Scripts\python.exe .\scripts\demo_adaptive.py --output output/evaluation-adaptive-examples-v2
```

2026-10-22：旧版充分性18/18、动作17/20，未追问且缺流程时直接联网。新版首轮出现2次过度拦截及1份矛盾输出被拒收；修正“不得自行增加用户未问的细节”后，充分性18/18、动作20/20。三个版本全部保留。该提示词在同组题上调整，属于开发验证，不能作为独立测试准确率。

实际补查示例将q008证据组从2/3补齐至3/3，零联网；实际入口对q010追问奖种，未调用回答模型；受控写反q003周/月工时的错误草稿被发现并改为每周8、每月40小时。受控错误是人为注入，不冒充真实回答故障。汇总报告：`output/evaluation-crag-summary-v1/report.txt`；各版本记录与实际演示分别在`output/evaluation-crag-*/`、`output/evaluation-adaptive-examples-v1/`。
