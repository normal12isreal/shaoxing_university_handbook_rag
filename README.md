# 绍兴大学学生手册 RAG 问答

基于学生手册的知识库问答，支持混合检索、重排、证据判断和带页码引用的回答，可直接运行脚本或作为 Python 包调用，无需常驻后台服务。

## 安装准备

- 安装 Python 3.11 或更高版本。
- 下载本仓库完整内容；已附带手册、分块和向量索引，无需重新解析 PDF、生成向量或下载 MinerU。
- 准备百炼北京区域的 API Key 和对应请求地址，并确保账号可调用 `config/` 中设置的模型。

以下命令在项目根目录的 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
New-Item -ItemType Directory -Path .runtime -Force | Out-Null
if (-not (Test-Path .runtime/dashscope.local.json)) {
    Copy-Item config/dashscope.example.json .runtime/dashscope.local.json
}
```

打开 `.runtime/dashscope.local.json`，填写两项配置：

```json
{
  "api_key": "",
  "base_url": ""
}
```

`api_key` 填自己的密钥；`base_url` 填与密钥匹配的请求地址，例如 `https://dashscope.aliyuncs.com/compatible-mode/v1`，也支持百炼北京工作空间的 `https://<工作空间域名前缀>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`。不要追加 `/chat/completions`，当前不支持其他厂商的地址。

该本地配置文件不会被 Git 跟踪。也可以用 `DASHSCOPE_API_KEY` 和 `DASHSCOPE_BASE_URL` 环境变量提供配置。

## 使用方式

### 终端问答

```powershell
.\.venv\Scripts\python.exe scripts/answer_question.py --question "申请国家奖学金需要满足哪些条件？"
```

不写 `--question` 时，会在终端提示输入问题。默认开启证据判断、按需网页补查和草稿复核，返回回答及引用页码。

常用开关可组合使用：

| 参数 | 作用 |
| --- | --- |
| `--no-web` | 关闭网页补查，模型 API 仍需联网 |
| `--no-crag` | 关闭证据判断、自适应路由及网页补查 |
| `--no-reflection` | 关闭草稿复核及其可能触发的一次修订 |
| `--retrieval-offline` | 检索只使用已有缓存；缺缓存时报错，后续阶段仍可能调用 API |
| `--dry-run` | 使用已有检索缓存保存提示词，不生成回答、不调用 API；仍需有效格式的配置 |

例如，仅关闭网页补查：

```powershell
.\.venv\Scripts\python.exe scripts/answer_question.py --question "图书可以借多久？" --no-web
```

模型 API 调用可能产生费用；`--no-web` 不等于完全离线运行。

### 在 Python 中调用

在已安装本包的 Python 环境中：

```python
from student_handbook_rag import HandbookRAG

rag = HandbookRAG(project_dir="下载后的项目目录")
result = rag.ask("申请国家奖学金需要满足哪些条件？", allow_web=False)
print(result["answer"])
print(result["sources"])
```

`allow_web=False` 关闭网页补查；`crag=False` 关闭证据判断及网页补查；`reflection=False` 关闭草稿复核。同一个 `rag` 对象可以连续调用 `ask()`，程序结束即可退出。
