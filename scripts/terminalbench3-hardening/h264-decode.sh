BOOTSTRAP_TASK=h264-decoder
cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/terminalbench3_review2/_flattened \
--task-ids $BOOTSTRAP_TASK \
-c ../datasets/gemini-terminus2-harden.yaml \
--solver-model gemini/gemini-3-flash-preview \
--hacker-model gemini/gemini-3-flash-preview \
--fixer-model gemini/gemini-3-flash-preview \
--solver-privileged \
--hacker-privileged \
--hacker-privileged-disable-iteration 8 \
--max-concurrent 100 \
--solver-max-turns 120 \
--hacker-max-turns 120 \
--fixer-max-turns 120 \
--solver-precheck-retries 4 \
--solver-timeout-multiplier 10.0 \
--hacker-timeout-multiplier 10.0 \
--pool-enabled \
--hacker-feedback \
--pool-bootstrap-dir=../datasets/terminalbench3_review2/_flattened/$BOOTSTRAP_TASK/ \
--fixer-prompt-file=./prompts/fixer_guidance.md \
--fixer-prompt-after-iter 5 \
--output-dir outputs/terminalbench3_h264-decoder \
