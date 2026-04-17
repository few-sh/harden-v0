"""Hodoscope pipeline adapter for harden-kb hacker trajectories.

Collapses each successful hack (one hacker_iter*_a* job with speedup >= threshold,
compiled, correct) into a single hodoscope trajectory so one point on the
visualization plane corresponds to one hack.
"""

from .pack import JobRecord, iter_successful_hacks, build_trajectory
from .prompt import RH_SUMMARIZE_PROMPT
from .run import analyze_batch

__all__ = [
    "JobRecord",
    "iter_successful_hacks",
    "build_trajectory",
    "RH_SUMMARIZE_PROMPT",
    "analyze_batch",
]
