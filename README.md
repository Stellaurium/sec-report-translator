# SEC Report Translator

[中文 README](doc/README_zh.md)

SEC Report Translator is a command-line tool for downloading SEC filings, unpacking `full-submission.txt` archives, and translating SEC HTML filing documents while preserving their rendered structure.

The translator is designed for annual reports and similar SEC filings that contain long narrative sections, tables, inline formatting, links, and referenced images. It uses OpenAI-compatible chat completion APIs, validates model output before writing it back into the HTML, and records detailed translation reports for review.

## Features

- Download SEC filings by ticker and form type.
- Unpack SEC `full-submission.txt` files into flat, browser-friendly document folders.
- Translate one HTML filing at a time.
- Preserve HTML tables, links, images, and most inline formatting.
- Skip numeric-only and low-value cells conservatively.
- Validate model output for IDs, placeholders, replacement characters, and numeric tokens.
- Retry failed translations with stricter formatting, repair prompts, and local mini-block context.
- Optionally use a fallback OpenAI-compatible model for units that still fail.
- Cache completed translation units on disk so interrupted runs can resume without starting over.
- Use block-based shared prompts and warmup requests to improve provider-side prompt/KV cache hit rates.
- Write JSON and Markdown reports with summary, failure reasons, retained units, fallback units, and token usage.

## Installation

```powershell
git clone https://github.com/your-org/sec-report-translator.git
cd sec-report-translator
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
```

After installation, the command is available as:

```powershell
.\.venv\Scripts\sec-translator.exe --help
```

## Quick Start

Create a configuration file:

```powershell
.\.venv\Scripts\sec-translator.exe init-config -o sec-translator.toml
```

Edit `sec-translator.toml` and fill in:

- `[model].base_url`
- `[model].api_key`
- `[model].model`
- `[sec].user_agent_name`
- `[sec].user_agent_email`

Download filings:

```powershell
.\.venv\Scripts\sec-translator.exe download --ticker PDD --form 20-F --limit 1 -o downloads --config sec-translator.toml
```

Unpack one downloaded submission:

```powershell
.\.venv\Scripts\sec-translator.exe unpack downloads\sec-edgar-filings\PDD\20-F\0000000000-00-000000\full-submission.txt -o output\PDD-20F
```

Translate the main HTML document:

```powershell
.\.venv\Scripts\sec-translator.exe translate output\PDD-20F\main-report.htm --config sec-translator.toml
```

The default translated output is written next to the input with the suffix from `[output].default_suffix`, for example:

```text
main-report_translated.htm
main-report_translation_report.json
main-report_translation_report.md
```

## Commands

```text
sec-translator init-config -o sec-translator.toml [--overwrite]
sec-translator download --ticker TICKER --form FORM -o DIR --config sec-translator.toml [--after YYYY-MM-DD] [--before YYYY-MM-DD] [--limit N]
sec-translator unpack full-submission.txt -o DIR [--overwrite]
sec-translator translate report.htm --config sec-translator.toml [-o translated.htm] [--overwrite]
```

## Documentation

- [Chinese README](doc/README_zh.md)
- [Implementation Notes](doc/SEC财报智能翻译工具实施方案.md)
- [User Guide](doc/用户使用手册.md)
- [Installation and Deployment Guide](doc/安装部署手册.md)

## Safety Model

The translator is conservative. If a translated unit fails validation and cannot be repaired, the original source text is retained instead of writing a potentially corrupted translation. This means a completed output may still contain a small number of untranslated source segments, but it should not silently drop or alter numeric tokens.

## Development

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Generated filings, unpacked outputs, local config files, caches, and run logs are intentionally ignored by Git.
