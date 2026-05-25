CONFIG_TEMPLATE = """[model]
base_url = "https://api.deepseek.com/v1"
api_key = "YOUR_API_KEY"
model = "deepseek-v4-flash"
temperature = 0.1
top_p = 0.95
timeout_seconds = 300
max_retries = 3
context_window_tokens = 1000000
max_output_tokens = 16000

[fallback_model]
enabled = true
base_url = "http://localhost:1234/v1"
api_key = "lm-studio"
model = "qwen3.5-9b"
temperature = 0.1
top_p = 0.95
timeout_seconds = 300
max_retries = 1
context_window_tokens = 6000
max_output_tokens = 2000

[sec]
user_agent_name = "Your Name or Company"
user_agent_email = "your_email@example.com"

[batch]
initial_units = 20
min_units = 1
max_units = 80
stable_successes_to_grow = 5
grow_factor = 1.25
shrink_factor = 0.5
max_chars_per_batch = 12000
prev_context_chars = 3000
next_context_chars = 2000

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

[block]
enabled = true
max_input_ratio = 0.12
target_batch_units = 1
max_batch_chars = 80000
max_batches_per_block = 80
max_workers = 50
warmup_first = true
warmup_delay_seconds = 2.0
before_context_ratio = 0.10
block_body_ratio = 0.80
after_context_ratio = 0.10
"""
