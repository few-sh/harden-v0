BOOTSTRAP_TASK=hello-world
cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/mixed_tag_hackable_terminalbench \
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
--hacker-feedback \
--pool-bootstrap-dir=../datasets/mixed_tag_hackable_terminalbench/$BOOTSTRAP_TASK/ \
--output-dir outputs/terminalbench-ablations-baseline-hackerfeedback \
