cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/tag_hackable_2026_03_30_dataset_tasks \
--task-id 3d-model-format-legacy \
-c ../datasets/glm-5-terminus2.yaml \
--solver-model fireworks_ai/accounts/fireworks/models/glm-5p1 \
--hacker-model fireworks_ai/accounts/fireworks/models/glm-5p1 \
--fixer-model fireworks_ai/accounts/fireworks/models/glm-5 \
--solver-privileged \
--max-concurrent 95 \
--solver-max-turns 60 \
--hacker-max-turns 60 \
--fixer-max-turns 60 \
--solver-precheck-retries 4 \
--solver-timeout-multiplier 10.0 \
--hacker-timeout-multiplier 10.0
