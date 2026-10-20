# 学生手册 RAG 学习项目

目前只完成第 1 步：使用本地 MinerU 解析 PDF，保留原文、页码和结构化结果。尚未分块、Embedding、建立检索索引或接入生成模型。

## 当前结果

- 输入：`data/input/student-handbook-2025.pdf`，从桌面原文件复制，原文件未修改。
- 整本基础版：`output/flash-v1/markdown.md`，覆盖 244 个物理 PDF 页面。
- 结构化内容：`output/flash-v1/structured_content.json`。
- 完整中间协议：`output/flash-v1/middle_json.json`。
- 原始模型/原生解析输出：`output/flash-v1/model_output.json`。
- 图片素材：`output/flash-v1/images/`。
- 运行信息与检查：`output/flash-v1/run.json`。
- 单页对照：`output/basic-page37-v1/`（Basic/auto）与 `output/basic-ocr-page37-v1/`（Basic/ocr）。这两版仍存在同一表格行合并问题。

`flash-v1` 使用 Flash/TXT，直接读取 PDF 文本层，不运行 OCR 或视觉语言模型。整本解析、保存及结构检查实测约 11 秒，不包含安装和人工质量检查。

**这是一份提取试验基线，尚未验收为可直接用于问答的知识库。** 页数完整只说明页面索引覆盖完整，不能证明所有文字、表格和公式准确。第 37 页（印刷页码 21）的绩点表在基础版中出现数字行合并，需要更高质量解析或针对表格处理；第 82 页（印刷页码 66）的正文与章节顺序经抽查正常。具体记录见 `QUALITY_REVIEW.md`。

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
```

执行策略限制 `.ps1` 时，可以只对本次进程运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\extract.ps1 -OutputName flash-v2
```

脚本通过 MinerU 公开 Python SDK 的 `parse()` 和 `ParseResult.save()` 生成原生结果，不使用大模型改写手册内容。Flash 使用 `ocr_mode=txt`；Basic/Standard 使用 `ocr_mode=auto`。本次没有配置云端文档解析、远程 VLM 或 LLM 后处理。Basic 的模型文件由 ModelScope 下载到本项目 `.runtime/mineru/models/`，解析在本机进行。标准模式还需要额外的 VLM 权重，尚未安装和验证。

本次实测文件逻辑大小：源码约 48 MiB，运行环境约 561 MiB，按需下载模型约 248 MiB，正式输出约 14 MiB。连同原 PDF、运行缓存和保留的两份重试临时输出，项目约 900 MiB（约 0.88 GiB；不含系统包下载缓存与文件系统分配开销）。模型整包约 818 MiB，但本次只下载实际用到的文件，没有下载整包或 VLM。两份重试输出位于 `.runtime/incomplete-flash-v1*`，不加入 Git；清理请求被自动审批策略阻止，故保留原状。

`page_idx` 是从 0 开始的物理 PDF 页序；用户看到的物理 PDF 页码为 `page_idx + 1`。正文中印刷的页码是另一套编号，后续分块应单独保留，不使用固定偏移作为通用规则。Markdown 便于阅读，后续建立索引应优先消费含页序的结构化 JSON。

## 版本与许可

- 上游：[opendatalab/MinerU](https://github.com/opendatalab/MinerU)。
- MinerU 4.0.12，源码固定在 `b7f3dd0c5f6e6d66a298239ffd02b328b873918e`。
- DocVortex 0.5.16，Python 3.11.4，具体依赖锁定于 `requirements.lock.txt`。
- 本次个人本地学习用途无需购买 MinerU API 额度或商业许可，运行消耗自己的计算资源。上游当前许可证是 Apache 2.0 加附加条款，在线服务标识要求和达到商业门槛后的许可要求以 [LICENSE.md](https://github.com/opendatalab/MinerU/blob/b7f3dd0c5f6e6d66a298239ffd02b328b873918e/LICENSE.md) 为准；模型依其各自许可证使用。

## 后续学习顺序

先验收提取质量，再研究章节/条款分块、Embedding 和索引、召回、重排、证据约束下的回答与评测。当前没有把尚未检查的提取内容自动导入任何检索系统。
