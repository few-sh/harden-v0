# Terminal-Bench Reward-Hacking Hints

Test cases for probing Harbor / Terminal-Bench env robustness. Each file describes one
distinct hack pattern from public disclosures of agents gaming the verifier. A robust
env should catch all of them.

## Dependency-chain hijacks (verifier fetches/installs tooling at verify time)

| # | Hack | File |
|---|------|------|
| 01 | curl wrap → uv install hijack | [01-curl-wrap.md](01-curl-wrap.md) |
| 02 | pip wrap → trojanized pytest install | [02-pip-wrap.md](02-pip-wrap.md) |
| 03 | python wrap → intercept `python -m pytest` | [03-python-wrap.md](03-python-wrap.md) |
| 04 | Pre-installed `uvx` wrap | [04-uvx-wrap.md](04-uvx-wrap.md) |

## Verifier-environment hijacks (agent state leaks into the verifier phase)

| # | Hack | File |
|---|------|------|
| 05 | Daemon swap of `/tests/test.sh` | [05-test-script-daemon-swap.md](05-test-script-daemon-swap.md) |
| 06 | bash replacement | [06-bash-replacement.md](06-bash-replacement.md) |
| 07 | python3 replacement | [07-python3-replacement.md](07-python3-replacement.md) |
| 08 | `LD_PRELOAD` / `ld.so.preload` injection | [08-ld-preload.md](08-ld-preload.md) |

## Sources

- **Berkeley RDI, "Trustworthy Benchmarks (cont.)"** — rdi.berkeley.edu/blog/trustworthy-benchmarks-cont/. Public disclosure of the curl-wrap exploit chain (82/89 Terminal-Bench tasks) plus pip/python/uvx fallbacks for the remaining 7. Source for hints 01–04.
- **harbor-framework/harbor#974, "Agents gaming the environment container"** — github.com/harbor-framework/harbor/issues/974. Maintainer issue enumerating in-container persistence vectors (test-script swap, shell/interpreter replacement, `LD_PRELOAD`) and proposing a separate verifier container. Source for hints 05–08.
