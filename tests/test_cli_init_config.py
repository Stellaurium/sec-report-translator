import subprocess
import sys

from sec_report_translator.config import load_config


def run_cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "sec_report_translator.cli", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
    )


def test_init_config_writes_template_and_refuses_overwrite(tmp_path):
    config_path = tmp_path / "sec-translator.toml"

    first = run_cli("init-config", "-o", str(config_path), cwd=tmp_path)

    assert first.returncode == 0, first.stderr
    text = config_path.read_text(encoding="utf-8")
    assert "[model]" in text
    assert 'api_key = "YOUR_API_KEY"' in text
    assert "[sec]" in text
    assert 'user_agent_email = "your_email@example.com"' in text
    assert "[batch]" in text
    assert "[translation]" in text
    assert "[output]" in text

    second = run_cli("init-config", "-o", str(config_path), cwd=tmp_path)

    assert second.returncode != 0
    assert "--overwrite" in second.stderr


def test_init_config_overwrite_replaces_existing_file(tmp_path):
    config_path = tmp_path / "sec-translator.toml"
    config_path.write_text("old config", encoding="utf-8")

    result = run_cli("init-config", "-o", str(config_path), "--overwrite", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    text = config_path.read_text(encoding="utf-8")
    assert "old config" not in text
    assert "[model]" in text


def test_config_file_with_utf8_bom_is_accepted(tmp_path):
    config_path = tmp_path / "sec-translator.toml"
    config_path.write_text(
        '\ufeff[sec]\nuser_agent_name = "Tester"\nuser_agent_email = "tester@example.com"\n',
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.sec.user_agent_email == "tester@example.com"
