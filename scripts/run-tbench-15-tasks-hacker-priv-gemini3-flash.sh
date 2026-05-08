cd ~/truthserum/harden-v0 \
&& uv run python harden.py --tasks-dir ../datasets/tag_hackable_2026_03_30_dataset_tasks \
--task-ids 3d-model-format-legacy,buffer-overflow-exploit,cobol-modernization,configure-git-webserver,git-repo-forensics,mongodb-sales-aggregation-engine,optimize-postgresql-analytics-query,play-lord,recover-and-sanitize-postgres-wal-secret,recover-corrupted-sqlite-data,recover-prod-db-password-from-git-history,sanitize-jinja2-ssti-templates,vul-flask,titanic-female-survival-prediction,schedule-vacation \
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
--output-dir outputs/tbench-15task-hacker-priv-gemini-3-flash-lockstep
