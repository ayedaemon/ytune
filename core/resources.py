"""
Reads this process's actual cgroup memory/CPU limits (as set by `deploy.resources.limits`
in docker-compose.yaml) so worker concurrency can size itself to the container it's
actually running in, instead of a fixed number picked once that silently drifts out of
sync with whatever memory limit someone sets later — that mismatch is exactly what let
ytdlp-downloader's 3-way concurrency exceed its 512M limit and get OOM-killed.
"""
from __future__ import annotations

import math
import os


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            value = f.read().strip()
    except OSError:
        return None
    if value in ("max", "-1"):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def cgroup_memory_limit_bytes() -> int | None:
    """Effective memory limit for this container, or None if unset/unlimited/unreadable."""
    v2 = _read_int("/sys/fs/cgroup/memory.max")
    if v2 is not None:
        return v2
    v1 = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if v1 is not None and v1 < (1 << 62):  # cgroup v1's "unlimited" is a huge sentinel, not -1
        return v1
    return None


def cgroup_cpu_limit() -> float | None:
    """Effective CPU core allotment (e.g. 1.5 cores), or None if unset/unlimited/unreadable."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().strip().split()
        if quota == "max":
            return None
        return int(quota) / int(period)
    except (OSError, ValueError):
        pass

    quota = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota is not None and quota > 0 and period:
        return quota / period
    return None


def suggest_concurrency(mem_per_worker_mb: int, min_workers: int = 1, max_workers: int = 8) -> int:
    """
    Pick a worker count that fits both the container's memory limit (assuming each
    worker can use up to mem_per_worker_mb) and its CPU allotment, clamped to
    [min_workers, max_workers]. Falls back to max_workers on any limit that can't be
    read (bare-metal, no cgroup, cgroup v1 without the right files mounted) — same
    ceiling today's hardcoded defaults effectively used.
    """
    mem_limit = cgroup_memory_limit_bytes()
    by_memory = max(1, mem_limit // (mem_per_worker_mb * 1024 * 1024)) if mem_limit else max_workers

    cpu_limit = cgroup_cpu_limit()
    if cpu_limit is not None:
        by_cpu = max(1, math.ceil(cpu_limit))
    else:
        by_cpu = os.cpu_count() or max_workers

    return max(min_workers, min(by_memory, by_cpu, max_workers))
