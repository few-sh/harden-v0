cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/tag_hackable_2026_03_30_dataset_tasks \
--task-id simple-web-scraper \
-c ../datasets/gemini-terminus2-harden.yaml \
--solver-model gemini/gemini-3-flash-preview \
--hacker-model gemini/gemini-3-flash-preview \
--fixer-model gemini/gemini-3-flash-preview \
--solver-privileged \
--max-concurrent 15 \
--solver-max-turns 60 \
--hacker-max-turns 60 \
--fixer-max-turns 60 \
--solver-precheck-retries 4 \
--solver-timeout-multiplier 10.0 \
--hacker-timeout-multiplier 10.0 \
--pool-enabled \
--pool-bootstrap-dir=../datasets/tag_hackable_2026_03_30_dataset_tasks/simple-web-scraper/ \
--output-dir outputs/single-task-simple-web-scraper-gemini-3-flash-1task
