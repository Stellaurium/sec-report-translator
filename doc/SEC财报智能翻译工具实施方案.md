# SEC Report Translator 实现说明

本文说明 SEC Report Translator 的实现方式、模块边界、核心流程和当前行为。内容以当前代码为准，面向维护者、二次开发者和需要评估系统行为的用户。

## 1. 项目目标

SEC Report Translator 是一个本地命令行工具，用于处理 SEC filing 工作流中的三个常见任务：

1. 按 ticker 和 form type 下载 filing。
2. 将 SEC `full-submission.txt` 拆解为可直接查看的独立文件。
3. 将 filing 中的主 HTML 文档翻译为目标语言，同时尽量保持 HTML 的原始渲染结构。

项目不是在线服务，也不维护任务数据库。它以本地文件为输入和输出，适合一次处理一份或少量 filing。

## 2. 技术栈

- Python 3.10+
- `argparse`：命令行解析
- `tomli`：TOML 配置读取
- `beautifulsoup4` + `lxml`：HTML 解析和重建
- `openai`：调用 OpenAI-compatible chat completions API
- `sec-edgar-downloader`：下载 SEC filing
- `pytest`：自动化测试

## 3. 目录结构

```text
sec_report_translator/
  cli.py          命令行入口
  config.py       配置文件结构、默认值解析、校验
  defaults.py     init-config 输出的 TOML 模板
  downloader.py   SEC filing 下载封装
  unpacker.py     full-submission.txt 拆包
  prompts.py      翻译 prompt 集中定义
  providers.py    OpenAI-compatible API 调用和 usage 统计
  translator.py   HTML 翻译、缓存、验证、重试、报告
tests/            自动化测试
doc/              项目文档
pyproject.toml    包配置和命令行入口
```

## 4. 命令行架构

入口位于 `sec_report_translator.cli:main`，安装后暴露为 `sec-translator`。

当前支持四个子命令：

- `init-config`：写入配置模板。
- `download`：按 ticker/form 下载 SEC filing。
- `unpack`：拆解 `full-submission.txt`。
- `translate`：翻译单个 HTML 文件。

所有命令失败时返回状态码 `2`，并将错误信息写入 stderr。输出文件已存在时，默认报错；需要显式传入 `--overwrite`。

## 5. 配置模型

配置文件由 `config.py` 解析为 `AppConfig`，包含以下配置块：

- `[model]`：主 LLM 配置。
- `[fallback_model]`：可选兜底模型配置。
- `[sec]`：SEC 下载所需 User-Agent 信息。
- `[batch]`：非 block 模式和部分 block 自适应策略使用的 batch 参数。
- `[translation]`：翻译策略。
- `[output]`：输出命名和报告开关。
- `[cache]`：磁盘翻译缓存。
- `[block]`：block 翻译、并发和上下文缓存友好策略。

程序不读取环境变量覆盖 API key。模型、SEC 身份信息和行为参数都由配置文件显式控制。

## 6. 下载实现

`downloader.py` 对 `sec-edgar-downloader` 做了一层薄封装。

流程：

1. 从配置读取 `[sec].user_agent_name` 和 `[sec].user_agent_email`。
2. 创建 `sec_edgar_downloader.Downloader`。
3. 调用 `get(form_type, ticker, after, before, limit, download_details=True)`。
4. 校验目标目录存在、下载数量不是 0、没有 0 字节文件。

下载路径由底层库生成，通常形如：

```text
downloads/sec-edgar-filings/PDD/20-F/<accession-number>/full-submission.txt
```

`--after`、`--before` 和 `--limit` 会直接传给底层库。一般可用来限制日期范围和下载数量。

## 7. 拆包实现

`unpacker.py` 解析 SEC `full-submission.txt` 的 SGML-like 结构。

核心规则：

- 从 `<SEC-HEADER>...</SEC-HEADER>` 中提取 filing 元信息。
- 从每个 `<DOCUMENT>...</DOCUMENT>` 块中提取：
  - `<SEQUENCE>`
  - `<TYPE>`
  - `<FILENAME>`
  - `<DESCRIPTION>`
  - `<TEXT>`
- 所有文件采用扁平结构写入输出目录。
- 原始 `full-submission.txt` 也会复制到输出目录。
- 额外生成 `manifest.json`，记录 header、文档列表、类型、大小和解码状态。

内容分类依据文件扩展名和文档类型：

- HTML：`.htm`、`.html`、`.xhtml`
- 图片：`.jpg`、`.jpeg`、`.png`、`.gif`、`.bmp`、`.webp` 或 `TYPE=GRAPHIC`
- XBRL：`.xsd` 或 `TYPE` 以 `EX-101` 开头
- XML、TXT、常见二进制文件

图片和二进制内容会尝试按 uuencode 或 base64 解码；无法识别时保留原始字节/文本。扁平输出使 HTML 对同目录图片的相对引用通常可以直接生效。

## 8. HTML 翻译单元提取

`translator.py` 使用 BeautifulSoup 解析 HTML，并从有限的 block 标签中提取翻译单元：

```text
p, li, td, th, caption, h1-h6, div
```

以下内容会跳过：

- `script`、`style`、`meta`、`link`、`head`、`noscript`、`code`、`pre`
- 嵌套了更细 block 候选元素的外层容器
- 空文本、纯数字、过短或保护性术语
- 隐藏或低价值的 iXBRL/元数据节点

提取时会尽量选择语义完整的块。对于含有内联标签的段落，程序会把内联元素替换为占位符，例如 `<PH_1>...</PH_1>`，要求模型在译文中保留这些占位符。写回 HTML 时，占位符会恢复为原有标签，因此加粗、链接、脚注等内联结构可以跟随语义移动。

## 9. 翻译请求格式

模型需要返回 JSON，支持两种形式：

```json
[
  {"id": "u_0001", "text": "译文"}
]
```

或：

```json
{
  "translations": [
    {"id": "u_0001", "text": "译文"}
  ]
}
```

单个翻译单元重试时，也允许单个对象：

```json
{"id": "u_0001", "text": "译文"}
```

程序会从 Markdown code block 或额外文本中提取 JSON 片段，但更推荐模型直接返回纯 JSON。

## 10. 验证规则

每次模型输出写回前必须通过验证：

- ID 顺序必须与输入一致。
- 返回条数必须与请求单元数一致。
- 译文不能为空。
- 译文不能包含 Unicode replacement character `�`。
- 不能出现模型拒绝回答文本。
- 内联占位符数量和名称必须与源文本一致。
- 源文本中的数字 token 必须在译文中保留。

如果验证失败，程序不会直接写入错误译文。

## 11. 重试和兜底策略

当前 block 模式下，单个 batch 的处理顺序是：

1. 使用 block 公共上下文请求主模型。
2. 如果输出格式类错误，使用更严格 JSON 要求重试。
3. 如果仍失败且错误可修复，发送 repair 请求，附带失败原因和上一次模型输出，让模型修正。
4. 如果 repair 失败，构造 mini-block：当前 batch 前后各最多 2 个 batch，使用较小上下文再次请求主模型。
5. 如果配置了 fallback 模型，失败单元会延后交给 fallback 模型处理。
6. 如果 fallback 未启用或也失败，保留原文并写入报告。

对于 context length/token limit 类错误，block 大小会缩小并重试；如果缩到最低仍失败，会退回线性翻译。

## 12. Block 模式和上下文缓存

Block 模式用于处理大文档和降低重复输入成本。

概念层级：

```text
translation unit -> batch -> block -> LLM request
```

默认配置中：

- `target_batch_units = 1`，即每个 batch 通常只翻译一个单元。
- `max_batches_per_block = 80`，即一个 block 最多包含 80 个 batch。
- block 公共 prompt 包含：
  - 前文摘要上下文
  - 当前 block 的全部 batch
  - 后文摘要上下文
  - JSON 输出规则

每个 batch 请求都携带相同的 block 公共 prompt，只改变“当前要翻译哪个 batch”。这样服务端如果支持前缀/KV 缓存，后续 batch 可以命中大部分公共输入。

`warmup_first = true` 时，每个 block 会先发送一个只包含公共 prompt 的预热请求，然后等待 `warmup_delay_seconds`，再翻译第一个 batch，最后并发翻译剩余 batch。这有助于让支持前缀缓存的服务端先建立缓存。

## 13. 并发模型

Block 内部使用 `ThreadPoolExecutor` 并发处理 batch。最大并发由 `[block].max_workers` 控制，实际并发不会超过当前 block 的 batch 数。

建议：

- 云端高并发模型可以使用较大的 `max_workers`。
- 如果遇到单个请求长时间挂住、429 或网络不稳定，可以降低 `max_workers` 并缩短 `timeout_seconds`。
- 一般情况下，`20` 到 `50` 是较实用的范围。

## 14. 磁盘缓存

磁盘缓存用于中断恢复和重复运行。

缓存键包含：

- HTML 文件内容 hash
- prompt 版本和系统 prompt hash
- 目标语言
- 主模型名称
- 翻译单元文本 hash

缓存文件默认位于输入 HTML 同目录的 `.sec-translator-cache/` 下，文件名由 HTML hash 派生。缓存采用 JSONL 记录，不使用 SQLite。

这种方案足够简单，适合单文件翻译任务；同时它可以避免数据库迁移、锁和额外运行依赖。

注意：如果 HTML 文件内容、模型名、目标语言或 prompt 版本变化，旧缓存不会被复用。

## 15. 输出和报告

默认输出：

```text
report.htm
report_translated.htm
report_translation_report.json
report_translation_report.md
```

如果显式指定 `-o custom.htm`，报告会以自定义输出路径为基准：

```text
custom.htm
custom_translation_report.json
custom_translation_report.md
```

报告包含：

- 输入/输出文件
- 主模型和 fallback 模型摘要，API key 会被掩码
- 总翻译单元数
- 磁盘缓存命中数
- 主模型成功数
- repair 成功数
- mini-block 成功数
- fallback 成功数
- 保留原文数
- failure reasons
- provider token usage
- prompt cache hit/miss tokens 和命中率
- fallback、repair、mini-block、retained 单元列表

## 16. 表格处理原则

表格单元按 `td`/`th` 提取，但整体策略偏保守：

- 纯数字单元通常跳过。
- 数字 token 必须被保留。
- 短标题、会计科目、风险术语可以翻译。
- 如果模型改变或丢失数字，当前单元不会写入译文。

这样可以减少财务报表表格被破坏的风险。

## 17. 当前限制

- 只翻译用户指定的单个 HTML 文件，不自动判断 full-submission 中哪个 HTML 是主文档。
- 不翻译 PDF、图片、XBRL 或 exhibits。
- HTML 会由 BeautifulSoup/lxml 重建，源码文本不保证逐字符一致，但渲染结构应保持等价。
- provider-side prompt cache 是否命中取决于服务端；程序只能尽量构造稳定公共前缀，并在报告中记录服务端返回的 cache usage。
- fallback 模型只在启用时使用。

## 18. 测试覆盖

测试主要覆盖：

- 配置初始化
- 下载参数和结果校验
- full-submission 拆包
- HTML 结构保留
- 输出覆盖保护
- 磁盘缓存复用和失效
- block 大小受上下文窗口限制
- 并发 batch
- JSON 格式重试
- repair 和 mini-block 重试
- fallback 和保留原文
- prompt 结构约束

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest
```
