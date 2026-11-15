"""Compile the 32-bit helper (Probe.cs -> Probe.Mcp.exe) with the .NET Framework C# compiler.

    python build_helper.py --project projects/my_mod.json

crossover/wine: runs csc.exe inside the configured bottle/prefix.
native (Windows): runs %WINDIR%\\Microsoft.NET\\Framework\\v4.0.30319\\csc.exe directly (tested on Windows 11).
The repository has no prebuilt Probe.Mcp.exe: build it once (and again after Probe.cs changes). Only the geck_*
tools need it; the fnv_* tools do not.
"""
import argparse
import os
from pathlib import Path
import subprocess
from bridge_lock import bottle_lock
from geck_mcp.config import Project

CSC = 'C:\\windows\\Microsoft.NET\\Framework\\v4.0.30319\\csc.exe'

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--project', type=Path, default=root / 'projects' / 'example-crossover.json')
p = Project.load(parser.parse_args().project)
args = ['/nologo', '/platform:x86', '/out:' + p.editor_path(p.helper), p.editor_path(root / 'Probe.cs')]
if p.launcher == 'crossover':
    command = [str(p.wine), '--bottle', p.bottle.name, '--no-update', '--cx-app', CSC, *args]
elif p.launcher == 'wine':
    command = [str(p.wine), CSC, *args]
else:
    command = [os.path.expandvars(CSC.replace('C:\\windows', '%WINDIR%')), *args]
with bottle_lock(p.lock_root):
    subprocess.run(command, check=True, timeout=60, env=dict(os.environ, **p.helper_env()))
