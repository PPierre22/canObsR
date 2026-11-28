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
    """
    Memory that a step may use (GB): max_memory_gb, or the smallest of 75 % of the RAM and 85 % of the RAM available
    now (the other programs open can use a lot: 12-16 GB out of 34 on a laptop, 26/11/2026); None if unknown.
    """
    if max_memory_gb not in (None, 'auto'):
        return float(max_memory_gb)
    total, avail = ram_gb()
    if not total:
        return None
    return min(0.75 * total, 0.85 * avail) if avail else 0.75 * total


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


BALANCED_PLAN = '381b4222-f694-41f0-9685-ff5bb260df2e'   # Windows power plans (same GUIDs in every language)
HIGH_PERFORMANCE_PLAN = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'


def power_plan():
    """Windows: GUID of the active power plan (powercfg /getactivescheme), None elsewhere or if unknown."""
    if sys.platform != 'win32':
        return None
    try:
        import re
        import subprocess
        out = subprocess.run(['powercfg', '/getactivescheme'], capture_output=True, text=True, timeout=20,
                             creationflags=0x08000000).stdout
        m = re.search(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', out)
        return m.group(0).lower() if m else None
    except Exception:  # noqa: BLE001
        return None


def power_plan_note():
    """
    Text of a note if a computer without battery (desktop, server) uses the Balanced power plan, else ''. On a
    Windows server (verzy, 2 x Xeon Silver 4216, 28/11/2026), one core was 3-4 x slower than on a recent laptop
    in pure Python; the plan "High performance" may help (to measure with machine_diagnose.R).
    """
    p = power_status()
    if not p or p.get('has_battery') or power_plan() != BALANCED_PLAN:
        return ''
    return ('the Windows power plan is "Balanced" on a computer without battery: the plan "High performance" may '
            'speed up ODM (as administrator: powercfg /setactive ' + HIGH_PERFORMANCE_PLAN + '; on a shared server, '
            'ask its administrator)')


def disk_free_gb(paths):
    """Free space (GB) of the disk of each path (its first existing parent): {path: GB or None}."""
    import shutil
    out = {}
    for p in paths:
        q = os.path.abspath(p)
        while q and not os.path.exists(q) and os.path.dirname(q) != q:
            q = os.path.dirname(q)
        try:
            out[p] = round(shutil.disk_usage(q).free / 1e9, 1)
        except Exception:  # noqa: BLE001
            out[p] = None
    return out


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


def _pool_failure(e):
    """True if `e` is a failure of a process pool (worker killed, out of memory, Windows handle limit...)."""
    names = {c.__name__ for c in type(e).__mro__}
    return bool(names & {'BrokenProcessPool', 'BrokenExecutor', 'MemoryError'}) or \
        'un-serialize' in str(e) or 'need at most 63 handles' in str(e)


def with_fewer_processes(fn, n, log=print, what='AROSICS'):
    """
    Runs fn(n). If its process pool breaks (seen on a 64-thread Windows server: "BrokenProcessPool: A task has
    failed to un-serialize"), runs it again with n // 4 processes, then with 1, instead of failing.
    """
    tried = []
    for k in dict.fromkeys([max(1, int(n)), max(1, int(n) // 4), 1]):
        try:
            if tried:
                log(f'    {what}: process pool failed with {tried[-1]} processes, new try with {k}')
            return fn(k)
        except Exception as e:  # noqa: BLE001
            if not _pool_failure(e) or k == 1:
                raise
            tried.append(k)
