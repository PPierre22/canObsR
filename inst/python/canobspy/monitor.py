"""
Resource monitor: samples, every few seconds, the use of the processors, of the RAM and of the NVIDIA GPU of the
computer, with the step of the processing that is running (epoch, ODM stage, program), and writes them to a CSV
file. summarize() gives, per step, its duration and how much of the computer it used: the steps that leave the
computer idle are those where more parallelism would gain time. No dependency: Windows and Linux system calls,
nvidia-smi.
"""
import csv
import os
import sys
import threading
import time

from .resources import nvidia_status, ram_gb

# what is running, updated by the processing (timesift_odm.run_odm)
STATE = {'epoch': '', 'stage': '', 'step': ''}
FIELDS = ['time', 'elapsed_s', 'epoch', 'stage', 'step', 'cpu_pct', 'ram_used_gb', 'ram_total_gb', 'gpu_util',
          'gpu_mem_mb', 'gpu_power_w']


def _cpu_times():
    """(busy, total) processor times since boot, any unit; None if unknown."""
    try:
        if sys.platform == 'win32':
            import ctypes

            class FT(ctypes.Structure):
                _fields_ = [('lo', ctypes.c_ulong), ('hi', ctypes.c_ulong)]
            idle, kernel, user = FT(), FT(), FT()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
            v = lambda f: (f.hi << 32) | f.lo          # noqa: E731
            total = v(kernel) + v(user)                # kernel time includes the idle time
            return total - v(idle), total
        with open('/proc/stat') as f:
            x = [int(a) for a in f.readline().split()[1:]]
        idle = x[3] + (x[4] if len(x) > 4 else 0)
        return sum(x) - idle, sum(x)
    except Exception:  # noqa: BLE001
        return None


class Monitor:
    """with Monitor('resources.csv'): ... samples every `every` seconds in a background thread."""

    def __init__(self, path, every=5):
        self.path, self.every = path, every
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop()

    def start(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        new = not os.path.isfile(self.path)
        self._f = open(self.path, 'a', newline='', encoding='utf-8')
        self._w = csv.writer(self._f)
        if new:
            self._w.writerow(FIELDS)
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(self.every + 30)
        self._f.close()

    def _run(self):
        last = _cpu_times()
        while not self._stop.wait(self.every):
            now = _cpu_times()
            cpu = ''
            if last and now and now[1] > last[1]:
                cpu = round(100 * (now[0] - last[0]) / (now[1] - last[1]), 1)
            last = now
            total, avail = ram_gb()
            g = nvidia_status()
            g = g[0] if g else {}
            self._w.writerow([time.strftime('%Y-%m-%d %H:%M:%S'), round(time.time() - self._t0), STATE['epoch'],
                              STATE['stage'], STATE['step'], cpu,
                              round(total - avail, 2) if total and avail else '', round(total, 1) if total else '',
                              g.get('util', ''), g.get('mem_used_mb', ''), g.get('power_w', '')])
            self._f.flush()


def summarize(path, min_seconds=30):
    """
    Per step (epoch, ODM stage, program) of a monitor file: duration (min), mean and 95th percentile of the use of
    the processors (%), maximum RAM used (GB), mean use and maximum memory of the GPU. Returns a list of dicts in
    the order of the processing, and prints it.
    """
    rows = list(csv.DictReader(open(path, encoding='utf-8')))
    groups, order = {}, []
    for r in rows:
        k = (r['epoch'], r['stage'], r['step'])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)

    def num(xs):
        return [float(x) for x in xs if x not in ('', None)]
    out = []
    for k in order:
        g = groups[k]
        t = num(r['elapsed_s'] for r in g)
        dur = (max(t) - min(t) + 5) if t else 0
        if dur < min_seconds:
            continue
        cpu = sorted(num(r['cpu_pct'] for r in g))
        ram = num(r['ram_used_gb'] for r in g)
        gu = num(r['gpu_util'] for r in g)
        gm = num(r['gpu_mem_mb'] for r in g)
        out.append({'epoch': k[0], 'stage': k[1], 'step': k[2], 'minutes': round(dur / 60, 1),
                    'cpu_mean': round(sum(cpu) / len(cpu)) if cpu else None,
                    'cpu_p95': round(cpu[int(0.95 * (len(cpu) - 1))]) if cpu else None,
                    'ram_max_gb': round(max(ram), 1) if ram else None,
                    'gpu_mean': round(sum(gu) / len(gu)) if gu else None,
                    'gpu_mem_max_mb': round(max(gm)) if gm else None})
    print(f'{"epoch":<22}{"stage":<20}{"step":<24}{"min":>7}{"CPU%":>7}{"p95":>6}{"RAM GB":>8}{"GPU%":>6}{"GPU MB":>8}')
    for o in out:
        print(f'{o["epoch"][:21]:<22}{o["stage"][:19]:<20}{o["step"][:23]:<24}{o["minutes"]:>7}'
              f'{o["cpu_mean"] if o["cpu_mean"] is not None else "":>7}{o["cpu_p95"] if o["cpu_p95"] is not None else "":>6}'
              f'{o["ram_max_gb"] if o["ram_max_gb"] is not None else "":>8}'
              f'{o["gpu_mean"] if o["gpu_mean"] is not None else "":>6}{o["gpu_mem_max_mb"] if o["gpu_mem_max_mb"] is not None else "":>8}')
    return out
