from sec_report_translator.prompts import SYSTEM_PROMPT, build_block_user_prompt, build_user_prompt


def test_system_prompt_is_readable_chinese_and_requires_json_array():
    assert "专业的 SEC 财报 HTML 翻译引擎" in SYSTEM_PROMPT
    assert "只输出合法 JSON" in SYSTEM_PROMPT
    assert "即使只有一个对象也必须使用数组" in SYSTEM_PROMPT
    assert "浣犳槸" not in SYSTEM_PROMPT


def test_strict_json_retry_prompt_adds_format_reminder():
    prompt = build_user_prompt(
        '[{"id":"u_0001","text":"Risk Factors"}]',
        target_language="Simplified Chinese",
        strict_json=True,
    )

    assert "上一次响应无法被程序解析" in prompt
    assert "只输出 JSON 数组" in prompt
    assert '[{"id":"u_0001","text":"Risk Factors"}]' in prompt


def test_block_user_prompt_keeps_common_prompt_and_limits_ids():
    prompt = build_block_user_prompt(
        "<translation_block>...</translation_block>",
        batch_id="batch_003",
        item_ids=["u_0100"],
        strict_json=True,
    )

    assert "<translation_block>...</translation_block>" in prompt
    assert "请只翻译 batch_003" in prompt
    assert "本次只处理这些 id：u_0100" in prompt
    assert "即使只有一个 id，也必须输出数组" in prompt


def test_block_common_prompt_can_prime_cache_without_current_task():
    from sec_report_translator.prompts import build_block_common_prompt

    prompt = build_block_common_prompt(
        '[{"batch_id":"batch_001","items":[{"id":"u_0001","text":"Risk Factors"}]}]',
        target_language="Simplified Chinese",
    )

    assert "<cache_warmup_rule>" in prompt
    assert "没有提供 current_task" in prompt
    assert "只输出空 JSON 数组 []" in prompt
