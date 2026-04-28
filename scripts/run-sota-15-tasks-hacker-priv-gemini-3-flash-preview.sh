cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/tag_hackable_2026_03_30_dataset_tasks \
--task-ids 1152,438,361,749,1235,230,630,22,265,930,66,494,1225,1219,883 \
-c ../datasets/gemini-terminus2-harden.yaml \
--solver-model gemini/gemini-3-flash-preview \
--hacker-model gemini/gemini-3-flash-preview \
--fixer-model gemini/gemini-3-flash-preview \
--solver-privileged \
--hacker-privileged \
--max-concurrent 15 \
--solver-max-turns 60 \
--hacker-max-turns 60 \
--fixer-max-turns 60 \
--solver-precheck-retries 4 \
--solver-timeout-multiplier 10.0 \
--hacker-timeout-multiplier 10.0 \
--pool-enabled \
--pool-bootstrap-dir=../datasets/tag_hackable_2026_03_30_dataset_tasks \
--output-dir outputs/sota-15task-hacker-priv-gemini-3-flash-lockstep
