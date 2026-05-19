BOOTSTRAP_TASK=binary-sprite-extraction
cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/terminalbench3_review2/_flattened \
--task-ids $BOOTSTRAP_TASK \
-c ../datasets/gemini-terminus2-harden.yaml \
--solver-model gemini/gemini-3.1-pro-preview \
--hacker-model gemini/gemini-3.1-pro-preview \
--fixer-model gemini/gemini-3.1-pro-preview \
--solver-privileged \
--hacker-privileged \
--hacker-privileged-disable-iteration 8 \
--max-concurrent 100 \
--solver-max-turns 60 \
--hacker-max-turns 60 \
--fixer-max-turns 60 \
--solver-precheck-retries 4 \
--solver-timeout-multiplier 10.0 \
--hacker-timeout-multiplier 10.0 \
--pool-enabled \
--hacker-feedback \
--pool-bootstrap-dir=../datasets/terminalbench3_review2/_flattened/$BOOTSTRAP_TASK/ \
--output-dir outputs/terminalbench3_review2_smoketest-gempro \
