import json
import threading
import time

from bs4 import BeautifulSoup

from sec_report_translator.cli import main
from sec_report_translator.config import load_config, require_model_config
from sec_report_translator.translator import (
    extract_translation_units,
    looks_like_refusal,
    make_translation_cache,
)


def write_config(path, *, fallback_enabled=True):
    path.write_text(
        f"""[model]
base_url = "http://primary.test/v1"
api_key = "primary-key"
model = "primary-model"
temperature = 0.1
top_p = 0.95
timeout_seconds = 30
max_retries = 1
context_window_tokens = 128000
max_output_tokens = 4096

[fallback_model]
enabled = {str(fallback_enabled).lower()}
base_url = "http://fallback.test/v1"
api_key = "fallback-key"
model = "fallback-model"
temperature = 0.1
top_p = 0.95
timeout_seconds = 30
max_retries = 1
context_window_tokens = 128000
max_output_tokens = 4096

[sec]
user_agent_name = "Tester"
user_agent_email = "tester@example.com"

[batch]
initial_units = 3
min_units = 1
max_units = 8
stable_successes_to_grow = 2
grow_factor = 1.5
shrink_factor = 0.5
max_chars_per_batch = 2000
prev_context_chars = 200
next_context_chars = 200

[translation]
target_language = "Simplified Chinese"
conservative_table_translation = true
fallback_to_source_on_failure = true
skip_numeric_cells = true
preserve_company_names = true

[output]
default_suffix = "_translated"
write_markdown_report = true
write_json_report = true

[cache]
enabled = true
cache_dir = ".sec-translator-cache"
""",
        encoding="utf-8",
    )


class QueueProvider:
    def __init__(self, config, translation=None):
        self.config = config

    def translate(
        self,
        units,
        context_before="",
        context_after="",
        block_prompt="",
        batch_id="",
        strict_json=False,
        repair_reason="",
        previous_response="",
    ):
        QueueProvider.calls.append(
            {
                "model": self.config.model,
                "ids": [unit.id for unit in units],
                "texts": [unit.text for unit in units],
                "context_before": context_before,
                "context_after": context_after,
                "block_prompt": block_prompt,
                "batch_id": batch_id,
                "strict_json": strict_json,
                "repair_reason": repair_reason,
                "previous_response": previous_response,
            }
        )
        response = QueueProvider.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def configure_fake_provider(monkeypatch, responses):
    QueueProvider.calls = []
    QueueProvider.responses = list(responses)
    monkeypatch.setattr("sec_report_translator.translator.OpenAICompatibleProvider", QueueProvider)
    return QueueProvider


def as_response(pairs):
    return json.dumps([{"id": key, "text": value} for key, value in pairs])


def append_block_config(
    path,
    *,
    enabled=True,
    target_batch_units=1,
    max_batches_per_block=3,
    max_workers=1,
    warmup_first=True,
    warmup_delay_seconds=0.0,
    context_window_tokens=128000,
    max_input_ratio=0.6,
):
    text = path.read_text(encoding="utf-8")
    text = text.replace("context_window_tokens = 128000", f"context_window_tokens = {context_window_tokens}", 1)
    text += f"""

[block]
enabled = {str(enabled).lower()}
max_input_ratio = {max_input_ratio}
target_batch_units = {target_batch_units}
max_batch_chars = 2000
max_batches_per_block = {max_batches_per_block}
max_workers = {max_workers}
warmup_first = {str(warmup_first).lower()}
warmup_delay_seconds = {warmup_delay_seconds}
before_context_ratio = 0.2
block_body_ratio = 0.6
after_context_ratio = 0.2
"""
    path.write_text(text, encoding="utf-8")


def test_translate_writes_default_output_reports_and_preserves_structure(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "report.html"
    write_config(config)
    source.write_text(
        """<html><head><title>Example</title></head><body>
<h1>Annual Report</h1>
<table><tr><th>Revenue</th><th>2024</th></tr><tr><td>Total liabilities</td><td>393,840</td></tr></table>
<p>See <a href="#item5">Item 5</a> for liquidity.</p>
<script>Revenue should not be touched.</script>
</body></html>""",
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
        [
            as_response(
                [
                    ("u_0001", "年度报告"),
                    ("u_0002", "收入"),
                    ("u_0003", "负债总额"),
                ]
            ),
            as_response([("u_0004", "有关<PH_0>第 5 项</PH_0>的流动性信息。")]),
        ],
    )

    status = main(["translate", str(source), "--config", str(config)])

    assert status == 0
    output = tmp_path / "report_translated.html"
    html = output.read_text(encoding="utf-8")
    assert "年度报告" in html
    assert "收入" in html
    assert "负债总额" in html
    assert "393,840" in html
    assert '<a href="#item5">第 5 项</a>' in html
    assert "Revenue should not be touched" in html
    assert len(provider.calls) == 2

    report = json.loads((tmp_path / "report_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["total_units"] == 4
    assert report["summary"]["primary_success"] == 4
    assert report["summary"]["fallback_success"] == 0
    assert report["summary"]["source_retained"] == 0
    assert "primary-key" not in json.dumps(report)
    assert (tmp_path / "report_translation_report.md").exists()


def test_translate_refuses_to_overwrite_default_output(tmp_path, monkeypatch, capsys):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "report.html"
    write_config(config)
    source.write_text("<html><body><p>Revenue</p></body></html>", encoding="utf-8")
    (tmp_path / "report_translated.html").write_text("existing", encoding="utf-8")
    configure_fake_provider(monkeypatch, [as_response([("u_0001", "收入")])])

    status = main(["translate", str(source), "--config", str(config)])

    captured = capsys.readouterr()
    assert status != 0
    assert "--overwrite" in captured.err
    assert (tmp_path / "report_translated.html").read_text(encoding="utf-8") == "existing"


def test_translate_retries_as_single_units_when_batch_validation_fails(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "report.html"
    write_config(config)
    source.write_text(
        "<html><body><p>Revenue 2024</p><p>Total assets 393,840</p></body></html>",
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
        [
            as_response([("u_0001", "收入 2025"), ("u_0002", "总资产 393,840")]),
            as_response([("u_0001", "收入 2024")]),
            as_response([("u_0002", "总资产 393,840")]),
        ],
    )

    status = main(["translate", str(source), "--config", str(config)])

    assert status == 0
    html = (tmp_path / "report_translated.html").read_text(encoding="utf-8")
    assert "收入 2024" in html
    assert "总资产 393,840" in html
    assert [call["ids"] for call in provider.calls] == [["u_0001", "u_0002"], ["u_0001"], ["u_0002"]]
    report = json.loads((tmp_path / "report_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["failure_reasons"]["number_validation_failed"] == 1


def test_translate_uses_fallback_only_after_single_unit_primary_failure(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "report.html"
    write_config(config)
    source.write_text("<html><body><p>Risk Factors</p></body></html>", encoding="utf-8")
    provider = configure_fake_provider(
        monkeypatch,
        [
            RuntimeError("primary rejected"),
            as_response([("u_0001", "风险因素")]),
        ],
    )

    status = main(["translate", str(source), "--config", str(config)])

    assert status == 0
    html = (tmp_path / "report_translated.html").read_text(encoding="utf-8")
    assert "风险因素" in html
    assert [call["model"] for call in provider.calls] == ["primary-model", "fallback-model"]
    report = json.loads((tmp_path / "report_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["fallback_success"] == 1
    assert report["fallback_units"][0]["id"] == "u_0001"


def test_translate_keeps_source_when_primary_and_fallback_fail(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "report.html"
    write_config(config)
    source.write_text("<html><body><p>Restricted disclosure text</p></body></html>", encoding="utf-8")
    configure_fake_provider(monkeypatch, [RuntimeError("primary rejected"), RuntimeError("fallback rejected")])

    status = main(["translate", str(source), "--config", str(config)])

    assert status == 0
    html = (tmp_path / "report_translated.html").read_text(encoding="utf-8")
    assert "Restricted disclosure text" in html
    report = json.loads((tmp_path / "report_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["source_retained"] == 1
    assert report["retained_units"][0]["id"] == "u_0001"


def test_translate_requires_config_and_suggests_init_config(tmp_path, capsys):
    source = tmp_path / "report.html"
    source.write_text("<html><body><p>Revenue</p></body></html>", encoding="utf-8")

    status = main(["translate", str(source)])

    captured = capsys.readouterr()
    assert status != 0
    assert "sec-translator init-config -o sec-translator.toml" in captured.err


def test_translate_skips_ixbrl_hidden_metadata_and_preserves_visible_table(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "ixbrl.html"
    write_config(config)
    source.write_text(
        """<html><body>
<div style="display:none;"><ix:header><ix:hidden>
<ix:nonNumeric name="dei:EntityRegistrantName">Hidden Company Name</ix:nonNumeric>
</ix:hidden></ix:header></div>
<table><tr><td style="color:red">Selected Financial Data</td><td>2024</td><td>12,345</td></tr></table>
</body></html>""",
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
        [as_response([("u_0001", "选定财务数据")])],
    )

    status = main(["translate", str(source), "--config", str(config)])

    assert status == 0
    html = (tmp_path / "ixbrl_translated.html").read_text(encoding="utf-8")
    assert "Hidden Company Name" in html
    assert "选定财务数据" in html
    assert "12,345" in html
    assert 'style="color:red"' in html
    assert provider.calls[0]["texts"] == ["Selected Financial Data"]


def test_translate_single_style_span_without_placeholder_pressure(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "span.html"
    write_config(config)
    source.write_text(
        '<html><body><p><span style="font-weight:bold">Annual Report</span></p></body></html>',
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
        [as_response([("u_0001", "年度报告")])],
    )

    status = main(["translate", str(source), "--config", str(config)])

    assert status == 0
    html = (tmp_path / "span_translated.html").read_text(encoding="utf-8")
    assert '<span style="font-weight:bold">年度报告</span>' in html
    assert provider.calls[0]["texts"] == ["Annual Report"]


def test_refusal_detection_does_not_reject_normal_financial_risk_translation():
    assert not looks_like_refusal("我们可能无法维持过去的增长率，且不能保证未来经营结果。")
    assert looks_like_refusal("抱歉，我无法协助翻译该内容。")


def test_translate_accepts_single_json_object_for_single_unit_retry(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "single.html"
    write_config(config)
    source.write_text("<html><body><p>Revenue 2024</p></body></html>", encoding="utf-8")
    provider = configure_fake_provider(
        monkeypatch,
        ['{"id":"u_0001","text":"收入 2024"}'],
    )

    status = main(["translate", str(source), "--config", str(config)])

    assert status == 0
    html = (tmp_path / "single_translated.html").read_text(encoding="utf-8")
    assert "收入 2024" in html


def test_translate_retries_primary_with_strict_json_before_fallback(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "strict-retry.html"
    write_config(config)
    source.write_text("<html><body><p>Risk Factors</p></body></html>", encoding="utf-8")
    provider = configure_fake_provider(
        monkeypatch,
        [
            "Risk Factors -> 风险因素",
            as_response([("u_0001", "风险因素")]),
        ],
    )

    status = main(["translate", str(source), "--config", str(config)])

    assert status == 0
    assert [call["model"] for call in provider.calls] == ["primary-model", "primary-model"]
    assert [call["strict_json"] for call in provider.calls] == [False, True]
    html = (tmp_path / "strict-retry_translated.html").read_text(encoding="utf-8")
    assert "风险因素" in html
    report = json.loads((tmp_path / "strict-retry_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["primary_success"] == 1
    assert report["summary"]["fallback_success"] == 0


def test_translate_rejects_replacement_character_and_keeps_source(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "damaged-output.html"
    write_config(config, fallback_enabled=False)
    source.write_text("<html><body><p>Risk Factors</p></body></html>", encoding="utf-8")
    provider = configure_fake_provider(monkeypatch, [as_response([("u_0001", "风险�因素")])])

    status = main(["translate", str(source), "--config", str(config), "--overwrite"])

    assert status == 0
    html = (tmp_path / "damaged-output_translated.html").read_text(encoding="utf-8")
    assert "Risk Factors" in html
    assert "\ufffd" not in html
    assert len(provider.calls) == 1
    report = json.loads((tmp_path / "damaged-output_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["source_retained"] == 1
    assert report["summary"]["failure_reasons"]["replacement_character_validation_failed"] == 1


def test_translate_reuses_disk_cache_on_second_run(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "cache.html"
    write_config(config)
    source.write_text("<html><body><p>Revenue</p><p>Risk Factors</p></body></html>", encoding="utf-8")
    provider = configure_fake_provider(
        monkeypatch,
        [as_response([("u_0001", "收入"), ("u_0002", "风险因素")])],
    )

    first = main(["translate", str(source), "--config", str(config), "--overwrite"])
    assert first == 0
    assert len(provider.calls) == 1

    provider = configure_fake_provider(monkeypatch, [])
    second = main(["translate", str(source), "--config", str(config), "--overwrite"])

    assert second == 0
    assert provider.calls == []
    html = (tmp_path / "cache_translated.html").read_text(encoding="utf-8")
    assert "收入" in html
    assert "风险因素" in html
    report = json.loads((tmp_path / "cache_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["cache_hits"] == 2
    cache_files = list((tmp_path / ".sec-translator-cache").glob("*.jsonl"))
    assert len(cache_files) == 1


def test_translate_custom_output_uses_output_stem_for_reports(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "report.html"
    output = tmp_path / "custom-result.html"
    write_config(config)
    source.write_text("<html><body><p>Revenue</p></body></html>", encoding="utf-8")
    (tmp_path / "report_translation_report.json").write_text("old report", encoding="utf-8")
    provider = configure_fake_provider(monkeypatch, [as_response([("u_0001", "收入")])])

    status = main(["translate", str(source), "-o", str(output), "--config", str(config)])

    assert status == 0
    assert output.exists()
    assert (tmp_path / "custom-result_translation_report.json").exists()
    assert (tmp_path / "custom-result_translation_report.md").exists()
    assert (tmp_path / "report_translation_report.json").read_text(encoding="utf-8") == "old report"
    assert len(provider.calls) == 1


def test_translate_cache_is_invalidated_when_source_changes(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "cache.html"
    write_config(config)
    source.write_text("<html><body><p>Revenue</p></body></html>", encoding="utf-8")
    configure_fake_provider(monkeypatch, [as_response([("u_0001", "收入")])])
    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    source.write_text("<html><body><p>Total assets</p></body></html>", encoding="utf-8")
    provider = configure_fake_provider(monkeypatch, [as_response([("u_0001", "总资产")])])
    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert len(provider.calls) == 1
    html = (tmp_path / "cache_translated.html").read_text(encoding="utf-8")
    assert "总资产" in html


def test_translate_cache_does_not_retranslate_cached_units_after_gap(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "cache-gap.html"
    write_config(config, fallback_enabled=False)
    source.write_text(
        "<html><body><p>Revenue</p><p>Sensitive disclosure</p><p>Total assets</p></body></html>",
        encoding="utf-8",
    )
    configure_fake_provider(
        monkeypatch,
        [
            RuntimeError("batch failed"),
            as_response([("u_0001", "收入")]),
            RuntimeError("model refused"),
            as_response([("u_0003", "总资产")]),
        ],
    )
    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    provider = configure_fake_provider(monkeypatch, [])
    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert provider.calls == []
    report = json.loads((tmp_path / "cache-gap_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["cache_hits"] == 3
    assert report["summary"]["source_retained"] == 1
    assert report["retained_units"][0]["failure_reason"] == "cached_source_retained"


def test_block_translation_reuses_identical_common_prompt_with_dynamic_batch_id(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "block.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=1, max_batches_per_block=3)
    source.write_text(
        "<html><body>"
        "<p>Revenue one</p><p>Revenue two</p><p>Revenue three</p>"
        "</body></html>",
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
        [
            as_response([("u_0001", "收入 one")]),
            as_response([("u_0002", "收入 two")]),
            as_response([("u_0003", "收入 three")]),
        ],
    )

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert [call["ids"] for call in provider.calls] == [["u_0001"], ["u_0002"], ["u_0003"]]
    assert provider.calls[0]["block_prompt"]
    assert provider.calls[0]["block_prompt"] == provider.calls[1]["block_prompt"]
    assert provider.calls[1]["block_prompt"] == provider.calls[2]["block_prompt"]
    assert [call["batch_id"] for call in provider.calls] == ["batch_001", "batch_002", "batch_003"]
    assert "Revenue one" in provider.calls[0]["block_prompt"]
    assert "Revenue two" in provider.calls[0]["block_prompt"]
    assert "Revenue three" in provider.calls[0]["block_prompt"]


def test_block_translation_uses_model_context_window_to_limit_block_size(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "small-window.html"
    write_config(config, fallback_enabled=False)
    append_block_config(
        config,
        target_batch_units=1,
        max_batches_per_block=10,
        context_window_tokens=350,
        max_input_ratio=0.95,
    )
    source.write_text(
        "<html><body>"
        + "".join(f"<p>Long revenue disclosure number {index} with repeated details.</p>" for index in range(1, 7))
        + "</body></html>",
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
        [as_response([(f"u_{index:04d}", f"收入 {index}")]) for index in range(1, 7)],
    )

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    first_prompt = provider.calls[0]["block_prompt"]
    first_block_calls = [call for call in provider.calls if call["block_prompt"] == first_prompt]
    assert 1 <= len(first_block_calls) < 6
    assert len({call["block_prompt"] for call in provider.calls}) > 1


def test_block_translation_falls_back_to_linear_batch_when_context_is_too_large(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "context-too-large.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=1, max_batches_per_block=3)
    source.write_text(
        "<html><body><p>Revenue one</p><p>Revenue two</p><p>Revenue three</p></body></html>",
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
        [
            RuntimeError("context length exceeded"),
            as_response([("u_0001", "收入 one")]),
            as_response([("u_0002", "收入 two")]),
            as_response([("u_0003", "收入 three")]),
        ],
    )

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert provider.calls[0]["block_prompt"]
    assert provider.calls[1]["block_prompt"]
    assert [call["ids"] for call in provider.calls[1:]] == [["u_0001"], ["u_0002"], ["u_0003"]]
    html = (tmp_path / "context-too-large_translated.html").read_text(encoding="utf-8")
    assert "收入 one" in html
    assert "收入 two" in html
    assert "收入 three" in html


def test_block_translation_reuses_disk_cache_on_second_run(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "block-cache.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=1, max_batches_per_block=2)
    source.write_text(
        "<html><body><p>Revenue one</p><p>Revenue two</p></body></html>",
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
        [
            as_response([("u_0001", "收入 one")]),
            as_response([("u_0002", "收入 two")]),
        ],
    )

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0
    assert len(provider.calls) == 2

    provider = configure_fake_provider(monkeypatch, [])
    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert provider.calls == []
    report = json.loads((tmp_path / "block-cache_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["cache_hits"] == 2


def test_block_translation_batches_fragmented_cache_misses_together(tmp_path, monkeypatch):
    config_path = tmp_path / "sec-translator.toml"
    source = tmp_path / "fragmented-cache.html"
    write_config(config_path, fallback_enabled=False)
    append_block_config(config_path, target_batch_units=1, max_batches_per_block=4, max_workers=2)
    source.write_text(
        "<html><body><p>Cached one</p><p>Revenue two</p><p>Cached three</p><p>Revenue four</p></body></html>",
        encoding="utf-8",
    )
    config = load_config(config_path)
    html = source.read_text(encoding="utf-8")
    units = extract_translation_units(BeautifulSoup(html, "lxml"))
    cache = make_translation_cache(source, html, config, require_model_config(config))
    assert cache is not None
    cache.put(units[0], "缓存 one", "primary-model")
    cache.put(units[2], "缓存 three", "primary-model")
    provider = configure_fake_provider(
        monkeypatch,
        [
            as_response([("u_0002", "收入 two")]),
            as_response([("u_0004", "收入 four")]),
        ],
    )

    assert main(["translate", str(source), "--config", str(config_path), "--overwrite"]) == 0

    assert sorted(call["ids"] for call in provider.calls) == [["u_0002"], ["u_0004"]]
    report = json.loads((tmp_path / "fragmented-cache_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["cache_hits"] == 2
    assert report["summary"]["primary_success"] == 2


def test_block_translation_shrinks_failed_batch_units_without_dropping_block_context(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "block-shrink.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=2, max_batches_per_block=2)
    source.write_text(
        "<html><body><p>Revenue one</p><p>Revenue two</p><p>Revenue three</p></body></html>",
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
        [
            RuntimeError("batch failed"),
            as_response([("u_0001", "收入 one")]),
            as_response([("u_0002", "收入 two")]),
            as_response([("u_0003", "收入 three")]),
        ],
    )

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert [call["ids"] for call in provider.calls] == [
        ["u_0001", "u_0002"],
        ["u_0001"],
        ["u_0002"],
        ["u_0003"],
    ]
    assert provider.calls[0]["block_prompt"]
    assert len({call["block_prompt"] for call in provider.calls}) == 1


def test_block_translation_repairs_failed_batch_before_fallback(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "block-repair.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=1, max_batches_per_block=1)
    source.write_text("<html><body><p>Revenue 2024</p></body></html>", encoding="utf-8")
    provider = configure_fake_provider(
        monkeypatch,
        [
            as_response([("u_0001", "收入 2025")]),
            as_response([("u_0001", "收入 2024")]),
        ],
    )

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert len(provider.calls) == 2
    assert provider.calls[1]["repair_reason"] == "number_validation_failed"
    assert "2025" in provider.calls[1]["previous_response"]
    html = (tmp_path / "block-repair_translated.html").read_text(encoding="utf-8")
    assert "收入 2024" in html
    report = json.loads((tmp_path / "block-repair_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["primary_success"] == 1
    assert report["summary"]["repair_success"] == 1
    assert report["repaired_units"][0]["id"] == "u_0001"


def test_block_translation_uses_mini_block_after_repair_failure(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "block-mini-retry.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=1, max_batches_per_block=7, max_workers=1)
    source.write_text(
        "<html><body>"
        "<p>Revenue 2021 A</p><p>Revenue 2022 B</p><p>Revenue 2023 C</p>"
        "<p>Revenue 2024 D</p><p>Revenue 2025 E</p><p>Revenue 2026 F</p>"
        "<p>Revenue 2027 G</p>"
        "</body></html>",
        encoding="utf-8",
    )
    provider = configure_fake_provider(
        monkeypatch,
            [
                as_response([("u_0001", "收入 2021 A")]),
                as_response([("u_0002", "收入 2022 B")]),
                as_response([("u_0003", "收入 2023 C")]),
                as_response([("u_0004", "收入 2020 D")]),
                as_response([("u_0004", "收入 2026 D")]),
                as_response([("u_0004", "收入 2024 D")]),
                as_response([("u_0005", "收入 2025 E")]),
                as_response([("u_0006", "收入 2026 F")]),
                as_response([("u_0007", "收入 2027 G")]),
            ],
        )

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert [call["ids"] for call in provider.calls] == [
            ["u_0001"],
            ["u_0002"],
            ["u_0003"],
            ["u_0004"],
            ["u_0004"],
            ["u_0004"],
            ["u_0005"],
            ["u_0006"],
            ["u_0007"],
        ]
    mini_call = provider.calls[5]
    assert mini_call["repair_reason"] == ""
    assert "u_0002" in mini_call["block_prompt"]
    assert "u_0003" in mini_call["block_prompt"]
    assert "u_0004" in mini_call["block_prompt"]
    assert "u_0005" in mini_call["block_prompt"]
    assert "u_0006" in mini_call["block_prompt"]
    assert "u_0001" not in mini_call["block_prompt"]
    assert "u_0007" not in mini_call["block_prompt"]
    html = (tmp_path / "block-mini-retry_translated.html").read_text(encoding="utf-8")
    assert "收入 2024 D" in html
    report = json.loads((tmp_path / "block-mini-retry_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["mini_block_success"] == 1
    assert report["mini_block_units"][0]["id"] == "u_0004"


def test_block_translation_runs_batches_in_parallel_when_workers_are_configured(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "block-parallel.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=1, max_batches_per_block=6, max_workers=3)
    source.write_text(
        "<html><body>"
        + "".join(f"<p>Revenue item {letter}</p>" for letter in ["A", "B", "C", "D", "E", "F"])
        + "</body></html>",
        encoding="utf-8",
    )

    class ConcurrentProvider:
        calls = []
        lock = threading.Lock()
        in_flight = 0
        max_seen = 0

        def __init__(self, config, translation=None):
            self.config = config

        def translate(self, units, context_before="", context_after="", block_prompt="", batch_id="", strict_json=False):
            with ConcurrentProvider.lock:
                ConcurrentProvider.in_flight += 1
                ConcurrentProvider.max_seen = max(ConcurrentProvider.max_seen, ConcurrentProvider.in_flight)
                ConcurrentProvider.calls.append({"ids": [unit.id for unit in units], "block_prompt": block_prompt})
            time.sleep(0.05)
            with ConcurrentProvider.lock:
                ConcurrentProvider.in_flight -= 1
            return as_response([(unit.id, f"{unit.text} translated") for unit in units])

    monkeypatch.setattr("sec_report_translator.translator.OpenAICompatibleProvider", ConcurrentProvider)

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert ConcurrentProvider.max_seen > 1
    assert sorted(call["ids"][0] for call in ConcurrentProvider.calls) == [f"u_{index:04d}" for index in range(1, 7)]
    report = json.loads((tmp_path / "block-parallel_translation_report.json").read_text(encoding="utf-8"))
    assert report["summary"]["primary_success"] == 6


def test_block_translation_warms_up_first_batch_before_parallel_requests(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "block-warmup.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=1, max_batches_per_block=5, max_workers=4)
    source.write_text(
        "<html><body>"
        + "".join(f"<p>Revenue item {index}</p>" for index in range(1, 6))
        + "</body></html>",
        encoding="utf-8",
    )

    class WarmupProvider:
        calls = []
        lock = threading.Lock()
        first_completed = False
        in_flight = 0
        max_seen_after_warmup = 0
        violations = []

        def __init__(self, config, translation=None):
            self.config = config

        def translate(self, units, context_before="", context_after="", block_prompt="", batch_id="", strict_json=False):
            with WarmupProvider.lock:
                if batch_id != "batch_001" and not WarmupProvider.first_completed:
                    WarmupProvider.violations.append(batch_id)
                WarmupProvider.in_flight += 1
                if WarmupProvider.first_completed:
                    WarmupProvider.max_seen_after_warmup = max(
                        WarmupProvider.max_seen_after_warmup,
                        WarmupProvider.in_flight,
                    )
                WarmupProvider.calls.append(batch_id)
            time.sleep(0.05)
            with WarmupProvider.lock:
                WarmupProvider.in_flight -= 1
                if batch_id == "batch_001":
                    WarmupProvider.first_completed = True
            return as_response([(unit.id, f"{unit.text} translated") for unit in units])

    monkeypatch.setattr("sec_report_translator.translator.OpenAICompatibleProvider", WarmupProvider)

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert WarmupProvider.calls[0] == "batch_001"
    assert WarmupProvider.violations == []
    assert WarmupProvider.max_seen_after_warmup > 1


def test_block_translation_primes_shared_prompt_before_batch_warmup(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "block-primer.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=1, max_batches_per_block=3, max_workers=2)
    source.write_text(
        "<html><body><p>Revenue item A</p><p>Revenue item B</p><p>Revenue item C</p></body></html>",
        encoding="utf-8",
    )

    class PrimerProvider:
        events = []
        lock = threading.Lock()
        primed = False

        def __init__(self, config, translation=None):
            self.config = config

        def prime_block_cache(self, block_prompt):
            with PrimerProvider.lock:
                PrimerProvider.events.append(("prime", block_prompt))
            time.sleep(0.02)
            with PrimerProvider.lock:
                PrimerProvider.primed = True

        def translate(self, units, context_before="", context_after="", block_prompt="", batch_id="", strict_json=False):
            with PrimerProvider.lock:
                assert PrimerProvider.primed
                PrimerProvider.events.append(("translate", batch_id))
            return as_response([(unit.id, f"{unit.text} translated") for unit in units])

    monkeypatch.setattr("sec_report_translator.translator.OpenAICompatibleProvider", PrimerProvider)

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert PrimerProvider.events[0][0] == "prime"
    assert PrimerProvider.events[1] == ("translate", "batch_001")


def test_translate_report_includes_provider_cache_usage(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "usage.html"
    write_config(config, fallback_enabled=False)
    append_block_config(config, target_batch_units=1, max_batches_per_block=1, max_workers=1)
    source.write_text("<html><body><p>Risk Factors</p></body></html>", encoding="utf-8")

    class UsageProvider:
        def __init__(self, config, translation=None):
            self.config = config

        def translate(self, units, context_before="", context_after="", block_prompt="", batch_id="", strict_json=False):
            return as_response([(unit.id, "风险因素") for unit in units])

        def usage_summary(self):
            return {
                "requests": 1,
                "prompt_tokens": 1000,
                "completion_tokens": 20,
                "total_tokens": 1020,
                "prompt_cache_hit_tokens": 800,
                "prompt_cache_miss_tokens": 200,
                "prompt_cache_hit_ratio": 0.8,
            }

    monkeypatch.setattr("sec_report_translator.translator.OpenAICompatibleProvider", UsageProvider)

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    report = json.loads((tmp_path / "usage_translation_report.json").read_text(encoding="utf-8"))
    assert report["usage"]["primary"]["prompt_cache_hit_tokens"] == 800
    markdown = (tmp_path / "usage_translation_report.md").read_text(encoding="utf-8")
    assert "cache_hit=800" in markdown


def test_fallback_translations_are_marked_in_html(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "fallback-marker.html"
    write_config(config, fallback_enabled=True)
    source.write_text("<html><head></head><body><p>Restricted disclosure</p></body></html>", encoding="utf-8")
    configure_fake_provider(monkeypatch, [RuntimeError("primary rejected"), as_response([("u_0001", "Fallback translated")])])

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    html = (tmp_path / "fallback-marker_translated.html").read_text(encoding="utf-8")
    assert "sec-translator-fallback" in html
    assert 'data-sec-translator="fallback"' in html
    assert "Fallback translated" in html


def test_block_translation_defers_fallback_until_primary_block_finishes(tmp_path, monkeypatch):
    config = tmp_path / "sec-translator.toml"
    source = tmp_path / "block-deferred-fallback.html"
    write_config(config, fallback_enabled=True)
    append_block_config(config, target_batch_units=1, max_batches_per_block=2, max_workers=1)
    source.write_text("<html><body><p>Restricted disclosure</p><p>Revenue</p></body></html>", encoding="utf-8")
    provider = configure_fake_provider(
        monkeypatch,
        [
            RuntimeError("primary rejected"),
            as_response([("u_0002", "收入")]),
            as_response([("u_0001", "Fallback translated")]),
        ],
    )

    assert main(["translate", str(source), "--config", str(config), "--overwrite"]) == 0

    assert [call["model"] for call in provider.calls] == ["primary-model", "primary-model", "fallback-model"]
    assert [call["ids"] for call in provider.calls] == [["u_0001"], ["u_0002"], ["u_0001"]]
    html = (tmp_path / "block-deferred-fallback_translated.html").read_text(encoding="utf-8")
    assert "Fallback translated" in html
    assert "收入" in html
