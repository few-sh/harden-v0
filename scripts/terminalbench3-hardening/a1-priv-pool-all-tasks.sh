BOOTSTRAP_TASK=binary-sprite-extraction
cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/terminalbench3_review2/_flattened \
--all \
--solver-model gemini/gemini-3-flash-preview \
--hacker-model gemini/gemini-3-flash-preview \
--fixer-model gemini/gemini-3-flash-preview \
--solver-privileged \
--hacker-privileged \
--hacker-privileged-disable-iteration 8 \
--max-concurrent 100 \
--solver-max-turns 200 \
--hacker-max-turns 200 \
--fixer-max-turns 200 \
--solver-precheck-retries 4 \
--solver-timeout-multiplier 2.0 \
--hacker-timeout-multiplier 2.0 \
--pool-enabled \
--hacker-feedback \
--pool-bootstrap-dir=../datasets/terminalbench3_review2/_flattened/$BOOTSTRAP_TASK/ \
--fixer-prompt-file=./prompts/fixer_guidance.md \
--fixer-prompt-after-iter 5 \
--output-dir outputs/terminalbench3_review2_latestharbor_alltasks \
