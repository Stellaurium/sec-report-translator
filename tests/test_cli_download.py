from pathlib import Path

from sec_report_translator.cli import main


def write_config(path: Path) -> None:
    path.write_text(
        """
[model]
base_url = "https://api.deepseek.com/v1"
api_key = "test"
model = "deepseek-v4-pro"

[sec]
user_agent_name = "Test User"
user_agent_email = "test@example.com"
""".strip(),
        encoding="utf-8",
    )


def test_download_uses_sec_config_and_forwards_arguments(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "sec-translator.toml"
    output_dir = tmp_path / "downloads"
    write_config(config_path)
    calls = []

    class FakeDownloader:
        def __init__(self, user_agent_name, user_agent_email, output_dir):
            calls.append(
                {
                    "user_agent_name": user_agent_name,
                    "user_agent_email": user_agent_email,
                    "output_dir": output_dir,
                }
            )

        def download(self, ticker, form_type, after=None, before=None, limit=None):
            calls.append(
                {
                    "ticker": ticker,
                    "form_type": form_type,
                    "after": after,
                    "before": before,
                    "limit": limit,
                }
            )
            return output_dir / "sec-edgar-filings" / ticker / form_type

    monkeypatch.setattr("sec_report_translator.cli.EdgarDownloader", FakeDownloader)

    status = main(
        [
            "download",
            "--ticker",
            "pdd",
            "--form",
            "20-f",
            "--limit",
            "1",
            "-o",
            str(output_dir),
            "--config",
            str(config_path),
        ]
    )

    captured = capsys.readouterr()
    assert status == 0
    assert "Download completed" in captured.out
    assert calls == [
        {
            "user_agent_name": "Test User",
            "user_agent_email": "test@example.com",
            "output_dir": output_dir,
        },
        {
            "ticker": "PDD",
            "form_type": "20-F",
            "after": None,
            "before": None,
            "limit": 1,
        },
    ]


def test_download_requires_sec_config(tmp_path, capsys):
    config_path = tmp_path / "sec-translator.toml"
    config_path.write_text(
        """
[model]
base_url = "https://api.deepseek.com/v1"
api_key = "test"
model = "deepseek-v4-pro"
""".strip(),
        encoding="utf-8",
    )

    status = main(
        [
            "download",
            "--ticker",
            "PDD",
            "--form",
            "20-F",
            "-o",
            str(tmp_path / "downloads"),
            "--config",
            str(config_path),
        ]
    )

    captured = capsys.readouterr()
    assert status != 0
    assert "[sec]" in captured.err
    assert "user_agent_email" in captured.err


def test_download_rejects_invalid_sec_email(tmp_path, capsys):
    config_path = tmp_path / "sec-translator.toml"
    config_path.write_text(
        """
[model]
base_url = "https://api.deepseek.com/v1"
api_key = "test"
model = "deepseek-v4-pro"

[sec]
user_agent_name = "Test User"
user_agent_email = "bad@example..com"
""".strip(),
        encoding="utf-8",
    )

    status = main(
        [
            "download",
            "--ticker",
            "PDD",
            "--form",
            "20-F",
            "-o",
            str(tmp_path / "downloads"),
            "--config",
            str(config_path),
        ]
    )

    captured = capsys.readouterr()
    assert status != 0
    assert "valid email" in captured.err
