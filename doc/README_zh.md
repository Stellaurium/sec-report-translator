# SEC Report Translator

SEC Report Translator 是一个命令行工具，用于下载 SEC filing、拆解 `full-submission.txt` 归档，并在尽量保持 HTML 渲染结构的前提下翻译 SEC HTML filing 文档。

该工具面向年报和类似 SEC filing 文档。这类文件通常包含长篇叙述、表格、内联格式、链接和图片引用。程序通过 OpenAI-compatible chat completions API 调用模型，在写回 HTML 前验证模型输出，并生成详细报告，方便复核。

## 功能特性

- 按 ticker 和 form type 下载 SEC filing。
- 将 SEC `full-submission.txt` 拆解为扁平、浏览器友好的文件目录。
- 一次翻译一个 HTML filing 文件。
- 尽量保留 HTML 表格、链接、图片和内联格式。
- 保守跳过纯数字和低价值单元格。
- 校验模型输出中的 ID、占位符、乱码替换字符和数字 token。
- 对失败翻译进行严格格式重试、repair 重试和 mini-block 局部上下文重试。
- 可选使用另一个 OpenAI-compatible 模型作为 fallback。
- 将已完成的翻译单元缓存到磁盘，中断后可以复用已有结果。
- 使用 block 公共 prompt 和 warmup 请求提升服务端 prompt/KV cache 命中率。
- 输出 JSON 和 Markdown 报告，记录摘要、失败原因、保留原文、fallback 和 token usage。

## 安装

```powershell
git clone https://github.com/your-org/sec-report-translator.git
cd sec-report-translator
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
```

安装后可以运行：

```powershell
.\.venv\Scripts\sec-translator.exe --help
```

## 快速开始

生成配置文件：

```powershell
.\.venv\Scripts\sec-translator.exe init-config -o sec-translator.toml
```

编辑 `sec-translator.toml`，至少填写：

- `[model].base_url`
- `[model].api_key`
- `[model].model`
- `[sec].user_agent_name`
- `[sec].user_agent_email`

下载 filing：

```powershell
.\.venv\Scripts\sec-translator.exe download --ticker PDD --form 20-F --limit 1 -o downloads --config sec-translator.toml
```

拆解下载得到的 submission：

```powershell
.\.venv\Scripts\sec-translator.exe unpack downloads\sec-edgar-filings\PDD\20-F\0000000000-00-000000\full-submission.txt -o output\PDD-20F
```

翻译主 HTML 文档：

```powershell
.\.venv\Scripts\sec-translator.exe translate output\PDD-20F\main-report.htm --config sec-translator.toml
```

默认译文输出在输入文件旁边，后缀来自 `[output].default_suffix`：

```text
main-report_translated.htm
main-report_translation_report.json
main-report_translation_report.md
```

## 命令

```text
sec-translator init-config -o sec-translator.toml [--overwrite]
sec-translator download --ticker TICKER --form FORM -o DIR --config sec-translator.toml [--after YYYY-MM-DD] [--before YYYY-MM-DD] [--limit N]
sec-translator unpack full-submission.txt -o DIR [--overwrite]
sec-translator translate report.htm --config sec-translator.toml [-o translated.htm] [--overwrite]
```

## 文档

- [实现说明](SEC财报智能翻译工具实施方案.md)
- [用户使用手册](用户使用手册.md)
- [安装部署手册](安装部署手册.md)

## 保守写回策略

翻译器采用保守策略。如果某个翻译单元未通过验证且无法修复，程序会保留原文，而不是写入可能破坏含义或数字的译文。因此，完成后的 HTML 可能仍包含少量未翻译片段，但不会静默丢失或篡改数字 token。

## 开发

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

下载文件、拆包结果、本地配置、缓存和运行日志都默认被 Git 忽略。
