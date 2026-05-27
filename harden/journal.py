"""Per-task hacker+fixer journal.

The journal gives the hacker and fixer cross-iteration memory:

  * `<task_output_dir>/journal.md` — compact, prompt-injected. Header +
    tail of last K compact iter entries + current defenses-in-force ledger.
  * `<task_output_dir>/journal/iter_<N>.md` — full per-iter summaries for
    drill-down (mounted into containers at /journal/).
  * `<task_output_dir>/journal/iter_<N>_compact.md` — pre-rendered compact
    entry (so the prompt-facing `journal.md` is just concatenation, no
    re-parsing of full summaries).
  * `<task_output_dir>/journal/defenses_in_force.md` — LLM-maintained
    ledger. Refreshed only on accepted-fix iters; reverted/replay-broken
    iters do not enter the ledger.

Writes go through `append_iter`, which is called once per iteration after
the outcome is known. The journal is per-task and never propagated through
the pool — the pool is the cross-task channel, the journal is intra-task
memory.
"""

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_JOURNAL_FILE = "journal.md"
_JOURNAL_DIR = "journal"
_DEFENSES_FILE = "defenses_in_force.md"
_DEFAULT_COMPACT_MAX_ITERS = 10
_LEDGER_MAX_CHARS = 8_000  # belt-and-braces; the LLM is also asked to keep it short


# ── Read API ──────────────────────────────────────────────────────────────────

def read_compact(task_output_dir: Path) -> str:
    """Return the full prompt-facing journal markdown, or empty string if none."""
    path = task_output_dir / _JOURNAL_FILE
    if not path.exists():
        return ""
    return path.read_text()


def read_defenses_in_force(task_output_dir: Path) -> str:
    """Return only the defenses-in-force ledger, or empty string if none."""
    path = task_output_dir / _JOURNAL_DIR / _DEFENSES_FILE
    if not path.exists():
        return ""
    return path.read_text()


def journal_dir(task_output_dir: Path) -> Path:
    """Path to the per-iter drill-down dir (mounted into containers at /journal/)."""
    return task_output_dir / _JOURNAL_DIR


# ── Compact-entry rendering ───────────────────────────────────────────────────

def _first_value_for(markdown: str, prefix: str) -> str:
    """Extract the value from a `Prefix: value` line in the structured summary.

    Returns empty string if not found. Used to build compact entries without
    needing to plumb the Pydantic models through the call chain.
    """
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _render_compact_entry(
    iteration: int,
    outcome: str,
    hack_reward: float | None,
    hack_summary: str | None,
    fix_summary: str | None,
    notes: str | None,
    failure_detail: str | None = None,
) -> str:
    """Render a 4-6 line compact entry for one iteration.

    Used both in journal.md (concatenated tail) and as the per-iter
    `iter_<N>_compact.md` cache file. ``failure_detail`` is a one-line
    extract of the validation error and appears as a **Failure** bullet so
    the next fixer can read it directly without drilling down.
    """
    lines: list[str] = [f"## Iter {iteration} — outcome: {outcome}"]
    if notes:
        lines.append(f"_{notes}_")
    lines.append("")

    if failure_detail:
        lines.append(f"- **Failure**: {failure_detail}")

    if hack_summary:
        strategy = _first_value_for(hack_summary, "Strategy:") or "(no strategy extracted)"
        reward_str = f"reward {hack_reward:.2f}" if hack_reward is not None else "reward n/a"
        lines.append(f"- **Hacker** ({reward_str}): {strategy}")
    else:
        lines.append("- **Hacker**: (no hack this iteration)")

    if fix_summary:
        defense = _first_value_for(fix_summary, "Defense:") or "(no defense extracted)"
        interacts = _first_value_for(fix_summary, "Interacts with prior defenses:")
        interacts_str = f" [{interacts}]" if interacts else ""
        files = _first_value_for(fix_summary, "Files modified:")
        files_str = f" — files: {files}" if files else ""
        lines.append(f"- **Fixer**{interacts_str}: {defense}{files_str}")
    else:
        lines.append("- **Fixer**: (no fix this iteration)")

    return "\n".join(lines) + "\n"


# ── Defenses-in-force ledger ──────────────────────────────────────────────────

_LEDGER_SYSTEM_PROMPT = (
    "You maintain a running ledger of defenses currently in force on a "
    "task verifier across an adversarial hardening loop. Each iteration, "
    "the fixer adds, replaces, weakens, or extends defenses. You are given "
    "the prior ledger and a single iteration's hack+fix summary with outcome, "
    "and you produce the new ledger.\n\n"
    "Rules:\n"
    "- Each ledger entry is one line: '<short defense name> — <mechanism> (iter <N>)'.\n"
    "- Keep the ledger under 10 entries. If new entries would push you over, "
    "  merge the least-specific or oldest entries.\n"
    "- If the new fix EXTENDS an existing entry (same attack class, stronger check), "
    "  update that entry in place and keep its original iter index.\n"
    "- If the new fix REPLACES an existing entry, remove the old one and add the new.\n"
    "- If the new fix WEAKENS an existing entry (the diff removed a check), "
    "  remove the weakened entry.\n"
    "- If the new fix is INDEPENDENT (new attack class), add a new entry.\n"
    "- Only update for accepted fixes. Reverted or replay-broken fixes must NOT "
    "  appear in the ledger.\n"
    "- Iter index in parentheses is the iteration the defense was first introduced."
)


async def _update_defenses_ledger(
    prior_ledger: str,
    iteration: int,
    hack_summary: str,
    fix_summary: str,
    outcome: str,
    model: str,
    reasoning_effort: str | None,
) -> str:
    """Ask the LLM to fold the new iter into the ledger.

    Returns markdown for the new ledger. Falls back to a deterministic
    "append a line" update if the LLM call fails.
    """
    try:
        import litellm
        from pydantic import BaseModel, Field

        class LedgerEntry(BaseModel):
            name: str = Field(..., description="Short defense name, ~2-5 words.")
            mechanism: str = Field(
                ..., description="One-sentence description of how this defense blocks attacks."
            )
            iter_introduced: int = Field(
                ..., description="Iteration index when this defense was first introduced."
            )

        class LedgerResponse(BaseModel):
            entries: list[LedgerEntry] = Field(
                ...,
                description="The new ledger. Ordered newest-introduced first. Max 10 entries.",
            )

        user_payload = {
            "prior_ledger": prior_ledger or "(empty)",
            "iteration": iteration,
            "hack_summary": hack_summary,
            "fix_summary": fix_summary,
            "outcome": outcome,
        }
        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": _LEDGER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload)},
            ],
            "response_format": LedgerResponse,
            "num_retries": 2,
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort

        response = await litellm.acompletion(**kwargs)
        parsed = LedgerResponse.model_validate_json(response.choices[0].message.content)

        lines = ["## Defenses in force", ""]
        for i, entry in enumerate(parsed.entries[:10], 1):
            lines.append(
                f"{i}. **{entry.name}** — {entry.mechanism} (iter {entry.iter_introduced})"
            )
        rendered = "\n".join(lines) + "\n"
        if len(rendered) > _LEDGER_MAX_CHARS:
            logger.warning(
                "Ledger LLM output exceeded %d chars (%d); truncating.",
                _LEDGER_MAX_CHARS, len(rendered),
            )
            rendered = rendered[:_LEDGER_MAX_CHARS]
        return rendered

    except Exception as exc:
        logger.warning("Ledger LLM update failed (%s); appending deterministic entry.", exc)
        defense = _first_value_for(fix_summary, "Defense:") or "(no defense extracted)"
        mechanism = _first_value_for(fix_summary, "Mechanism:") or "(no mechanism extracted)"
        appended = f"- **{defense}** — {mechanism} (iter {iteration})\n"
        if prior_ledger.strip():
            return prior_ledger.rstrip() + "\n" + appended
        return "## Defenses in force\n\n" + appended


# ── Write API ─────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    """Write atomically via tmp + rename so partial writes don't corrupt journal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def _write_fixer_patch(
    jdir: Path,
    iteration: int,
    fixer_trial: Path | None,
) -> None:
    """Write `journal/iter_<N>.patch` with this iter's fixer-committed diff.

    Computed as ``git diff --binary initial HEAD`` inside the fixer's
    artifacts repo. The ``initial`` tag is created at fixer container
    start (see workspace.prepare_fixer_environment), so it captures the
    state going into this iter — which equals the previous iter's
    accepted hardened state (rejected iters don't update hardened).
    ``--binary`` keeps adds/edits of binary files (e.g. reference
    archives) in the patch rather than reducing them to "Binary files
    differ."

    Best-effort: missing/broken artifacts dirs log a warning and skip the
    write; empty diffs (fixer committed only `.legitimate`, say) write no
    file. The journal must never fail because of patch generation.
    """
    if fixer_trial is None:
        return
    artifacts = fixer_trial / "artifacts"
    if not artifacts.is_dir():
        return
    try:
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(artifacts),
             "diff", "--binary", "initial", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        logger.warning(
            "Could not run git diff for iter %d patch (%s): %s",
            iteration, artifacts, exc,
        )
        return
    if result.returncode != 0:
        logger.warning(
            "git diff returned non-zero for iter %d patch (%s): %s",
            iteration, artifacts, result.stderr.strip(),
        )
        return
    if not result.stdout.strip():
        # Empty diff — e.g. fixer only committed `.legitimate` with no
        # actual content changes. Nothing useful to write.
        return
    jdir.mkdir(parents=True, exist_ok=True)
    _atomic_write(jdir / f"iter_{iteration}.patch", result.stdout)


def _render_full_iter(
    iteration: int,
    outcome: str,
    hack_reward: float | None,
    hack_summary: str | None,
    fix_summary: str | None,
    notes: str | None,
    failure_detail: str | None = None,
) -> str:
    """Render the full per-iter drill-down file content."""
    parts: list[str] = [f"# Iter {iteration} — outcome: {outcome}"]
    if notes:
        parts.append(f"\n{notes}\n")
    if failure_detail:
        parts.append(f"\n## Failure\n\n{failure_detail}\n")
    if hack_summary:
        reward_str = f" (reward {hack_reward:.2f})" if hack_reward is not None else ""
        parts.append(f"\n## Hacker{reward_str}\n\n{hack_summary}\n")
    else:
        parts.append("\n## Hacker\n\n(no hack this iteration)\n")
    if fix_summary:
        parts.append(f"\n## Fixer\n\n{fix_summary}\n")
    else:
        parts.append("\n## Fixer\n\n(no fix this iteration)\n")
    return "".join(parts)


def _rebuild_journal_md(
    task_output_dir: Path,
    compact_max_iters: int,
) -> None:
    """Regenerate journal.md from the per-iter compact files + current ledger.

    Scans for `iter_<N>_compact.md` (sorted by N), takes the last
    `compact_max_iters`, concatenates, then appends the ledger.
    """
    jdir = journal_dir(task_output_dir)
    if not jdir.is_dir():
        return

    compact_files: list[tuple[int, Path]] = []
    for p in jdir.glob("iter_*_compact.md"):
        # Strip "iter_" prefix and "_compact.md" suffix to recover the integer.
        stem = p.name[len("iter_"):-len("_compact.md")]
        try:
            compact_files.append((int(stem), p))
        except ValueError:
            continue
    compact_files.sort(key=lambda t: t[0])

    if not compact_files:
        return

    tail = compact_files[-compact_max_iters:]
    truncated = len(compact_files) > len(tail)

    parts: list[str] = [
        "# Hardening journal",
        "",
        "Per-iteration history of hacker attacks and fixer defenses on this task. "
        "Full per-iter summaries are at `/journal/iter_<N>.md` (read-only). "
        "Use this to avoid (a) repeating defenses already tried, "
        "(b) weakening defenses currently holding the line.",
        "",
    ]
    if truncated:
        parts.append(
            f"_Showing iters {tail[0][0]}–{tail[-1][0]} "
            f"(omitted {len(compact_files) - len(tail)} earlier iters; "
            f"see `/journal/` for the full set)._"
        )
        parts.append("")

    for _, path in tail:
        parts.append(path.read_text().rstrip())
        parts.append("")

    ledger = read_defenses_in_force(task_output_dir)
    if ledger.strip():
        parts.append(ledger.rstrip())
        parts.append("")

    _atomic_write(task_output_dir / _JOURNAL_FILE, "\n".join(parts))


async def append_iter(
    task_output_dir: Path,
    *,
    iteration: int,
    outcome: str,
    fix_applied: bool,
    hack_reward: float | None,
    hack_summary: str | None,
    fix_summary: str | None,
    fixer_trial: Path | None = None,
    notes: str | None = None,
    failure_detail: str | None = None,
    ledger_model: str | None = None,
    reasoning_effort: str | None = None,
    compact_max_iters: int = _DEFAULT_COMPACT_MAX_ITERS,
) -> None:
    """Record one iteration in the journal.

    Writes the full drill-down file and the compact entry, then regenerates
    `journal.md`. Iff `fix_applied` and `ledger_model` is set, also refreshes
    `defenses_in_force.md` via an LLM call.

    ``failure_detail`` is a one-line summary of why the fix was rejected
    (extracted upstream from ``previous_failure``); appears as a prominent
    bullet in the compact entry so the next fixer's prompt surfaces the
    specific blocker without needing to drill into ``iter_<N>.md``.
    """
    jdir = journal_dir(task_output_dir)
    jdir.mkdir(parents=True, exist_ok=True)

    full = _render_full_iter(
        iteration, outcome, hack_reward, hack_summary, fix_summary, notes, failure_detail,
    )
    compact = _render_compact_entry(
        iteration, outcome, hack_reward, hack_summary, fix_summary, notes, failure_detail,
    )
    _atomic_write(jdir / f"iter_{iteration}.md", full)
    _atomic_write(jdir / f"iter_{iteration}_compact.md", compact)
    _write_fixer_patch(jdir, iteration, fixer_trial)

    if fix_applied and ledger_model and hack_summary and fix_summary:
        prior = read_defenses_in_force(task_output_dir)
        new_ledger = await _update_defenses_ledger(
            prior, iteration, hack_summary, fix_summary, outcome,
            model=ledger_model, reasoning_effort=reasoning_effort,
        )
        _atomic_write(jdir / _DEFENSES_FILE, new_ledger)

    _rebuild_journal_md(task_output_dir, compact_max_iters=compact_max_iters)
