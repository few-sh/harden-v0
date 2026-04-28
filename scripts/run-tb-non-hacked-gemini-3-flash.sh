cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/TerminalBench-original-non-hacked \
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
--pool-bootstrap-dir=../datasets/TerminalBench-original-non-hacked \
--output-dir outputs/tb-non-hacked-gemini-3-flash
