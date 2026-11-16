"""PLAN-3 gate 4: the smoke suite in a loop for 30 minutes on one running game (attached), then a summary.

    .venv/Scripts/python.exe tools/fnv_soak.py [minutes=30] [out.json]

Run from bridge/ after fnv_game launch and 25 s idle (the MCP server then frees the pipe). Stops early on the first
failure or when the game process is gone. Private memory is sampled every 10 passes; judge flatness with
fnv_memory pools (committed bytes, live NiRefObjects) before and after some more passes, as PLAN-3 S7 did."""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
PY = str(BRIDGE / '.venv' / 'Scripts' / 'python.exe')
MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('soak.json')


class PMC(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD), ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t), ('PrivateUsage', ctypes.c_size_t)]


def game_pid():
    tasklist = Path(os.environ.get('SystemRoot') or r'C:\Windows') / 'System32' / 'tasklist.exe'
    out = subprocess.run([str(tasklist), '/FI', 'IMAGENAME eq FalloutNV.exe', '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True).stdout.strip()
    if not out or 'FalloutNV' not in out:
        return None
    return int(out.splitlines()[0].split('","')[1])


def private_mb(pid):
    h = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not h:
        return None
    try:
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return None
        return round(c.PrivateUsage / 1048576, 1)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


pid = game_pid()
start = time.time()
result = {'pid': pid, 'startMB': private_mb(pid) if pid else None, 'passes': 0, 'failures': [], 'memory': []}
while time.time() - start < MINUTES * 60:
    if game_pid() != pid:
        result['failures'].append({'pass': result['passes'] + 1, 'why': 'game process gone'})
        break
    t = time.time()
    run = subprocess.run([PY, 'tools/fnv_live_regress.py', '--attach', '--suite', 'smoke'], cwd=str(BRIDGE),
                         capture_output=True, text=True)
    tail = (run.stdout + run.stderr).strip().splitlines()[-3:]
    verdict = next((line for line in tail if line.startswith('smoke:')), '')
    if 'PASS' not in verdict:
        result['failures'].append({'pass': result['passes'] + 1, 'tail': tail})
        break
    result['passes'] += 1
    if result['passes'] % 10 == 0:
        result['memory'].append({'pass': result['passes'], 'minutes': round((time.time() - start) / 60, 1),
                                 'privateMB': private_mb(pid)})
    result['lastPassSeconds'] = round(time.time() - t, 1)
result['minutes'] = round((time.time() - start) / 60, 1)
result['endMB'] = private_mb(pid) if game_pid() == pid else None
result['gameAlive'] = game_pid() == pid
OUT.write_text(json.dumps(result, indent=1))
print(json.dumps({k: v for k, v in result.items() if k != 'memory'}))
