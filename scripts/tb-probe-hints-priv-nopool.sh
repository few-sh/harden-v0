cd ..
uv run ./probe_hints.py --all \
 --max-concurrent 100 \
 --envs pristine,propagated \
 --solver-attempts 3 \
 --hint-attempts 1 \
 --hacker-threshold 1.0 \
 --pristine-tasks-dir ~/truthserum/datasets/mixed_tag_hackable_terminalbench \
 --propagated-tasks-dir ~/truthserum/harden-v0/outputs/terminalbench-ablations-priv-nopool-hackerfeedback \
 --hints-dir ~/truthserum/harden-v0/scripts/hints-tbench
