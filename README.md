# 学生手册 RAG 学习项目

目前完成本地文档解析、已识别表格的逐页校对，以及首版程序化分块。尚未进行 Embedding、建立检索索引或接入生成模型。

## 当前结果

- **首版分块：`output/chunks-v1/chunks.jsonl`**，由 Python 规则程序从校对版全文自动生成，规则见下文；`chunks.txt` 是同一批结果的纯文本阅读副本。
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

覆盖校验只证明现有输入数据没有在分块时遗漏，不证明原始 PDF 提取本身全部正确，也不证明每一块都是最优检索单元。首版的表格前后短句可能各自成块、章节之间的规则关联也可能分开；后续用真实问题检查召回效果再调整规则，无须逐块人工重写。当前不是索引或检索效果验收。

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

# 检查已对照原图的关键数值与合并单元格约束
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

执行策略限制 `.ps1` 时，可以只对本次进程运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\extract.ps1 -OutputName flash-v2
```

脚本通过 MinerU 公开 Python SDK 的 `parse()` 和 `ParseResult.save()` 生成原生结果，不使用大模型改写手册内容。Flash 使用 `ocr_mode=txt`；Basic/Standard 使用 `ocr_mode=auto`。本次没有配置云端文档解析、远程 VLM 或 LLM 后处理。Basic 的模型文件由 ModelScope 下载到本项目 `.runtime/mineru/models/`，解析在本机进行。标准模式还需要额外的 VLM 权重，尚未安装和验证。

本次实测文件逻辑大小：源码约 48 MiB，运行环境约 561 MiB，按需下载模型约 248 MiB，正式输出约 14 MiB。连同原 PDF、运行缓存和保留的两份重试临时输出，项目约 900 MiB（约 0.88 GiB；不含系统包下载缓存与文件系统分配开销）。模型整包约 818 MiB，但本次只下载实际用到的文件，没有下载整包或 VLM。两份重试输出位于 `.runtime/incomplete-flash-v1*`，不加入 Git；清理请求被自动审批策略阻止，故保留原状。

`page_idx` 是从 0 开始的物理 PDF 页序；用户看到的物理 PDF 页码为 `page_idx + 1`。正文中印刷的页码是另一套编号，不使用固定偏移作为通用规则。分块以校对版 `reviewed-v1/document.json` 为输入；后续建立索引使用 `chunks-v1/chunks.jsonl`，不要继续把原提取版含错的表格用于问答。

## 版本与许可

- 上游：[opendatalab/MinerU](https://github.com/opendatalab/MinerU)。
- MinerU 4.0.12，源码固定在 `b7f3dd0c5f6e6d66a298239ffd02b328b873918e`。
- DocVortex 0.5.16，Python 3.11.4，具体依赖锁定于 `requirements.lock.txt`。
- 本次个人本地学习用途无需购买 MinerU API 额度或商业许可，运行消耗自己的计算资源。上游当前许可证是 Apache 2.0 加附加条款，在线服务标识要求和达到商业门槛后的许可要求以 [LICENSE.md](https://github.com/opendatalab/MinerU/blob/b7f3dd0c5f6e6d66a298239ffd02b328b873918e/LICENSE.md) 为准；模型依其各自许可证使用。

## 后续学习顺序

先检查规则分块的真实样本和粒度，再学习 Embedding 和索引、召回、重排、证据约束下的回答与评测。后续调整分块规则应重跑字符与表格行覆盖校验，不能把数字单独分块。当前没有自动导入任何检索系统。
