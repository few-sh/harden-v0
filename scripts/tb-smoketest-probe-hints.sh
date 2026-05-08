cd ..
uv run ./probe_hints.py \
 --task-ids chem-property-targeting \
 --max-concurrent 100 \
 --envs pristine,propagated \
 --solver-attempts 3 \
 --hint-attempts 1 \
 --nohint-attempts 3 \
 --hacker-threshold 1.0 \
 --no-kernelbench-mode \
 --pristine-tasks-dir ~/truthserum/datasets/mixed_tag_hackable_terminalbench \
 --propagated-tasks-dir ~/truthserum/harden-v0/outputs/terminalbench-ablations-baseline-hackerfeedback \
 --hints-dir ~/truthserum/harden-v0/scripts/hints-tbench
