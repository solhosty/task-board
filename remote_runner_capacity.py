#!/usr/bin/env python3
"""Report the usable process capacity of a Coder runner without changing it."""

import json
import os
from pathlib import Path


GIB = 1024 ** 3
MEMORY_PER_TASK = 2 * GIB


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def cpuset_count(value):
    total = 0
    for part in value.split(','):
        bounds = part.strip().split('-', 1)
        try:
            total += int(bounds[-1]) - int(bounds[0]) + 1
        except (TypeError, ValueError):
            return None
    return total or None


def cpu_limit():
    limits = [value for value in (os.cpu_count(), cpuset_count(read_text('/sys/fs/cgroup/cpuset.cpus.effective')),
                                  cpuset_count(read_text('/sys/fs/cgroup/cpuset/cpuset.cpus'))) if value]
    cpu_max = read_text('/sys/fs/cgroup/cpu.max').split()
    if len(cpu_max) == 2 and cpu_max[0] != 'max':
        quota_value, period_value = positive_int(cpu_max[0]), positive_int(cpu_max[1])
        if quota_value and period_value:
            limits.append(max(1, quota_value // period_value))
    else:
        quota_value = positive_int(read_text('/sys/fs/cgroup/cpu/cpu.cfs_quota_us'))
        period_value = positive_int(read_text('/sys/fs/cgroup/cpu/cpu.cfs_period_us'))
        if quota_value and period_value:
            limits.append(max(1, quota_value // period_value))
    return min(limits) if limits else 1


def memory_limit():
    for path in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
        value = read_text(path)
        if value and value != 'max':
            limit = positive_int(value)
            if limit:
                return limit
    for line in read_text('/proc/meminfo').splitlines():
        if line.startswith('MemTotal:'):
            value = positive_int(line.split()[1])
            return value * 1024 if value else None
    return None


cpu = cpu_limit()
memory = memory_limit()
memory_slots = max(1, memory // MEMORY_PER_TASK) if memory else 1
print(json.dumps({
    'cpu_count': cpu,
    'memory_bytes': memory,
    'memory_per_task_bytes': MEMORY_PER_TASK,
    'max_tasks': max(1, min(cpu, memory_slots)),
}))
