cd "$(dirname "$0")/.." \
&& uv run python harden.py --tasks-dir ../../datasets/tag_hackable_2026_02_09_dataset_tasks \
--task-ids adaptive-rejection-sampler \
-c ../../datasets/sonnet-terminus2-harden.yaml \
--solver-model anthropic/claude-sonnet-4-6 \
--hacker-model anthropic/claude-sonnet-4-6 \
--fixer-model anthropic/claude-sonnet-4-6 \
--solver-privileged \
--max-concurrent 10 \
--solver-max-turns 60 \
--hacker-max-turns 60 \
--fixer-max-turns 60 \
--solver-precheck-retries 4 \
--solver-timeout-multiplier 10.0 \
--hacker-timeout-multiplier 10.0
