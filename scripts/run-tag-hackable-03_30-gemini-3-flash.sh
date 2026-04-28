cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/tag_hackable_2026_03_30_dataset_tasks \
--all \
-c ../datasets/gemini-terminus2-harden.yaml \
--solver-model gemini/gemini-3-flash-preview \
--hacker-model gemini/gemini-3-flash-preview \
--fixer-model gemini/gemini-3-flash-preview \
--solver-privileged \
--max-concurrent 100 \
--solver-max-turns 60 \
--hacker-max-turns 60 \
--fixer-max-turns 60 \
--solver-precheck-retries 4 \
--solver-timeout-multiplier 10.0 \
--hacker-timeout-multiplier 10.0 \
--pool-enabled \
--pool-bootstrap-dir=../datasets/tag_hackable_2026_03_30_dataset_tasks \
--output-dir outputs/tb-tag-hackable-2026-03-30-gemini-3-flash-skip-limit
