from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, load_config, require_sec_config
from .defaults import CONFIG_TEMPLATE
from .downloader import DownloadError, EdgarDownloader
from .translator import TranslateError, translate_html_file
from .unpacker import UnpackError, unpack_submission


class CliError(Exception):
    pass


def write_text_file(path: Path, content: str, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise CliError(f"Output file already exists: {path}. Add --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def command_init_config(args: argparse.Namespace) -> int:
    write_text_file(Path(args.output), CONFIG_TEMPLATE, args.overwrite)
    return 0


def command_download(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    sec_config = require_sec_config(config)
    output_dir = Path(args.output_dir)
    downloader = EdgarDownloader(
        sec_config.user_agent_name,
        sec_config.user_agent_email,
        output_dir,
    )
    download_path = downloader.download(
        args.ticker.upper(),
        args.form_type.upper(),
        after=args.after,
        before=args.before,
        limit=args.limit,
    )
    print(f"Download completed: {download_path}")
    return 0


def command_unpack(args: argparse.Namespace) -> int:
    output_dir = unpack_submission(Path(args.input), Path(args.output_dir), overwrite=args.overwrite)
    print(f"Unpack completed: {output_dir}")
    return 0


def command_translate(args: argparse.Namespace) -> int:
    if not args.config:
        raise CliError(
            "translate requires --config. Run: sec-translator init-config -o sec-translator.toml"
        )
    config = load_config(Path(args.config))
    output = translate_html_file(
        Path(args.input),
        Path(args.output) if args.output else None,
        config,
        overwrite=args.overwrite,
    )
    print(f"Translation completed: {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sec-translator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_config = subparsers.add_parser("init-config", help="write a template TOML config file")
    init_config.add_argument("-o", "--output", required=True, help="config file to write")
    init_config.add_argument("--overwrite", action="store_true", help="overwrite an existing file")
    init_config.set_defaults(func=command_init_config)

    download = subparsers.add_parser("download", help="download SEC filings by ticker and form")
    download.add_argument("--ticker", required=True, help="ticker symbol, e.g. PDD")
    download.add_argument("--form", required=True, dest="form_type", help="SEC form type, e.g. 20-F")
    download.add_argument("--after", help="only filings after YYYY-MM-DD")
    download.add_argument("--before", help="only filings before YYYY-MM-DD")
    download.add_argument("--limit", type=int, help="maximum filings to download")
    download.add_argument("-o", "--output-dir", required=True, help="download root directory")
    download.add_argument("--config", required=True, help="TOML config file")
    download.add_argument("--overwrite", action="store_true", help="reserved for future use")
    download.set_defaults(func=command_download)

    unpack = subparsers.add_parser("unpack", help="unpack a SEC full-submission.txt file")
    unpack.add_argument("input", help="path to full-submission.txt")
    unpack.add_argument("-o", "--output-dir", required=True, help="directory to write flat files")
    unpack.add_argument("--overwrite", action="store_true", help="overwrite files produced by this command")
    unpack.set_defaults(func=command_unpack)

    translate = subparsers.add_parser("translate", help="translate one SEC HTML report file")
    translate.add_argument("input", help="path to an HTML file")
    translate.add_argument("-o", "--output", help="translated HTML output path")
    translate.add_argument("--config", help="TOML config file")
    translate.add_argument("--overwrite", action="store_true", help="overwrite generated output/report files")
    translate.set_defaults(func=command_translate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (CliError, ConfigError, DownloadError, UnpackError, TranslateError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
