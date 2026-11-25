"""
Processors and memory of the computer, and the number of parallel threads / processes chosen when the user leaves
n_cores = 'auto'. Every parallel step of canobspy needs memory per thread or process: the 'auto' value is the
smallest of (1) the processors minus a few left free for the system and the user, and (2) the memory allowed
(max_memory_gb, default 75 % of the RAM) divided by the memory needed per thread or process.
"""
import os
import sys


def ram_gb():
    """(total, available) RAM in GB; (None, None) if unknown."""
    try:
        if sys.platform == 'win32':
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('sullAvailExtendedVirtual', ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 1e9, st.ullAvailPhys / 1e9
        if sys.platform == 'darwin':
            import subprocess
            total = int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True).stdout) / 1e9
            return total, None
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, v = line.split(':', 1)
                info[k] = int(v.split()[0]) * 1024 / 1e9
        return info.get('MemTotal'), info.get('MemAvailable')
    except Exception:  # noqa: BLE001 - unknown: the memory limit is then not used
        return None, None


def cores():
    return os.cpu_count() or 1


def system_resources():
    total, avail = ram_gb()
    return {'cores': cores(), 'ram_total_gb': round(total, 1) if total else None,
            'ram_available_gb': round(avail, 1) if avail else None}


def free_cores(n):
    """Processors left free for the system and the user: 1 up to 4 processors, 2 up to 16, then 10 %."""
    return 1 if n <= 4 else 2 if n <= 16 else max(2, round(n * 0.1))


def memory_budget(max_memory_gb='auto'):
    """Memory that a step may use (GB): max_memory_gb, or 75 % of the RAM; None if unknown."""
    if max_memory_gb not in (None, 'auto'):
        return float(max_memory_gb)
    total, _ = ram_gb()
    return 0.75 * total if total else None


def auto_workers(per_worker_gb, n_cores='auto', max_memory_gb='auto', cap=None):
    """
    Number of threads or processes and the reason: n_cores if given (integer), else
    min(processors - free_cores, memory budget / per_worker_gb, cap).
    """
    if n_cores not in (None, 'auto'):
        n = max(1, int(n_cores))
        return (min(n, cap) if cap else n), f'{n} (n_cores)'
    c = cores()
    n_cpu = max(1, c - free_cores(c))
    budget = memory_budget(max_memory_gb)
    n_mem = int(budget // per_worker_gb) if budget else n_cpu
    n = max(1, min(n_cpu, n_mem, cap or n_cpu))
    why = (f'{n} (auto: {c} processors, {n_cpu} usable; '
           + (f'{budget:.0f} GB of memory allowed / {per_worker_gb:.1f} GB each = {n_mem}' if budget else 'memory unknown')
           + (f'; limit {cap}' if cap and cap < min(n_cpu, n_mem) else '') + ')')
    return n, why


def nvidia_smi():
    """Path of nvidia-smi, None if there is no NVIDIA driver."""
    import shutil
    p = shutil.which('nvidia-smi')
    if not p and sys.platform == 'win32':
        p = os.path.join(os.environ.get('SystemRoot', 'C:/Windows'), 'System32', 'nvidia-smi.exe')
    return p if p and os.path.isfile(p) else None


def nvidia_status():
    """List of dicts (name, mem_used_mb, mem_total_mb, util, power_w, power_limit_w) per NVIDIA GPU; [] if none."""
    import subprocess
    exe = nvidia_smi()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, '--query-gpu=name,memory.used,memory.total,utilization.gpu,power.draw,power.limit',
                              '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return []
    gpus = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(',')]

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None
        if len(f) >= 6:
            gpus.append({'name': f[0], 'mem_used_mb': num(f[1]), 'mem_total_mb': num(f[2]), 'util': num(f[3]),
                         'power_w': num(f[4]), 'power_limit_w': num(f[5])})
    return gpus


def power_status():
    """
    Windows: {'on_battery': bool, 'energy_saver': bool} (GetSystemPowerStatus: AC line, battery/energy saver);
    None elsewhere or if unknown. A laptop on battery or with the energy saver on can leave its NVIDIA GPU unused.
    """
    if sys.platform != 'win32':
        return None
    try:
        import ctypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [('ACLineStatus', ctypes.c_ubyte), ('BatteryFlag', ctypes.c_ubyte),
                        ('BatteryLifePercent', ctypes.c_ubyte), ('SystemStatusFlag', ctypes.c_ubyte),
                        ('BatteryLifeTime', ctypes.c_ulong), ('BatteryFullLifeTime', ctypes.c_ulong)]
        st = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        return {'on_battery': st.ACLineStatus == 0, 'energy_saver': bool(st.SystemStatusFlag & 1),
                'has_battery': st.BatteryFlag != 128}
    except Exception:  # noqa: BLE001
        return None


def power_warning():
    """Text of a warning if the computer may slow down the GPU (battery, energy saver), else ''."""
    p = power_status()
    if not p:
        return ''
    w = []
    if p['on_battery']:
        w.append('the computer runs on battery')
    if p['energy_saver']:
        w.append('the Windows energy saver is on')
    if not w:
        return ''
    return (' and '.join(w) + ': the NVIDIA GPU may be left unused or limited (dense matching 3-10 x slower). Plug '
            'the computer in, turn the energy saver off and choose Settings > System > Power > "Best performance".')
