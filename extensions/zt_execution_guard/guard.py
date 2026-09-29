"""Bounded VT maintenance commands; runtime journals and caches stay on E:."""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

PROJECT = Path('G:/My Drive/work/Investment-AI-Drive-Research')
RUNTIME = Path('E:/codex-runtime/investment-ai/vibe-trading')
THREAD_KEYS = ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
               'NUMEXPR_NUM_THREADS', 'NUMEXPR_MAX_THREADS')
VERSION = '1.0.0'


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_state(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def child_environment():
    env = os.environ.copy()
    for key in THREAD_KEYS:
        env[key] = '2'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONUTF8'] = '1'
    env['TEMP'] = env['TMP'] = str(RUNTIME / 'tmp')
    return env


def stop_owned_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def launch_arguments(command):
    if (os.name == 'nt' and len(command) == 4
            and Path(command[0]).name.lower() == 'cmd.exe'
            and command[1:3] == ['/d', '/c']):
        # cmd uses its own quote grammar; Python's CRT escaping would insert
        # literal backslashes before a quoted executable in the command body.
        return subprocess.list2cmdline(command[:1]) + ' /d /s /c "' + command[3] + '"'
    return command


def memory_status():
    if os.name != 'nt':
        return {'status': 'UNVERIFIED', 'reason': 'Windows commit check required'}
    class Info(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in (
                'CommitTotal', 'CommitLimit', 'CommitPeak', 'PhysicalTotal',
                'PhysicalAvailable', 'SystemCache', 'KernelTotal', 'KernelPaged',
                'KernelNonpaged', 'PageSize')] + [
            (name, ctypes.c_ulong) for name in ('HandleCount', 'ProcessCount', 'ThreadCount')]
    info = Info()
    info.cb = ctypes.sizeof(info)
    api = ctypes.WinDLL('psapi', use_last_error=True).GetPerformanceInfo
    api.argtypes = [ctypes.POINTER(Info), ctypes.c_ulong]
    api.restype = ctypes.c_int
    if not api(ctypes.byref(info), info.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    used, limit = info.CommitTotal * info.PageSize, info.CommitLimit * info.PageSize
    headroom = limit - used
    percent = 100 * used / limit
    return {'status': 'PASS' if percent < 90 and headroom >= 4 * 1024**3 else 'FAIL',
            'committed_bytes': used, 'commit_limit_bytes': limit,
            'headroom_bytes': headroom, 'commit_percent': round(percent, 2),
            'required': 'commit < 90% and at least 4 GiB headroom'}


def project_probe(project, timeout=10):
    # Run Drive I/O in a child whose wait is bounded; a blocked read cannot freeze the parent.
    code = ('import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); '
            'paths=[p.parent/"AGENTS.md",p/"AGENTS.md",p/"VT_UI.html"]; '
            'print(json.dumps({"readable":[str(x) for x in paths '
            'if x.open("rb").read(256)]}))')
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(project)],
                               cwd=str(RUNTIME / 'tmp'), env=child_environment(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    started = time.monotonic()
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_owned_tree(process)
        return {'status': 'FAIL', 'reason': 'DRIVE_READ_TIMEOUT', 'timeout_seconds': timeout}
    if process.returncode:
        return {'status': 'FAIL', 'reason': 'CANONICAL_DRIVE_UNAVAILABLE',
                'project': str(project), 'elapsed_seconds': round(time.monotonic()-started, 3)}
    return {'status': 'PASS', 'elapsed_seconds': round(time.monotonic()-started, 3),
            **json.loads(output)}


def preflight(project=PROJECT):
    drive = project_probe(project)
    try:
        memory = memory_status()
    except OSError as exc:
        memory = {'status': 'FAIL', 'reason': type(exc).__name__}
    return {'checked_at': utc(), 'drive': drive, 'memory': memory,
            'status': 'PASS' if drive['status'] == memory['status'] == 'PASS' else 'FAIL'}


def run_command(command, *, name, cwd, log_root, timeout=900, heartbeat=10,
                check=None):
    if not all(math.isfinite(x) and x > 0 for x in (timeout, heartbeat)):
        raise ValueError('timeout and heartbeat must be positive')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', name) or '..' in name:
        raise ValueError('unsafe step name')
    log_root = Path(log_root).resolve()
    if os.name == 'nt' and log_root.drive.upper() != 'E:':
        raise ValueError('runtime journals must be on E:')
    directory = log_root / (name + '-' + uuid4().hex[:12])
    directory.mkdir(parents=True)
    state = {'schema': 'vt-execution-guard/1', 'engine_version': VERSION,
             'name': name, 'started_at': utc(),
             'status': 'STARTING', 'timeout_seconds': timeout, 'cwd': str(cwd),
             'stdout': str(directory/'stdout.log'), 'stderr': str(directory/'stderr.log')}
    state_path = directory/'state.json'
    write_state(state_path, state)
    if check is None:
        check = preflight()
    state['preflight'] = check
    if check['status'] != 'PASS':
        state.update(status='PREFLIGHT_FAILED', exit_code=78, finished_at=utc())
        write_state(state_path, state)
        print(json.dumps({'status': state['status'], 'receipt': str(state_path)}), flush=True)
        return state
    started = time.monotonic()
    try:
        with (directory/'stdout.log').open('wb') as out, (directory/'stderr.log').open('wb') as err:
            process = subprocess.Popen(launch_arguments(command), cwd=str(cwd), env=child_environment(),
                                       stdout=out, stderr=err)
            state.update(status='RUNNING', pid=process.pid)
            write_state(state_path, state)
            next_heartbeat = started
            try:
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout:
                        stop_owned_tree(process)
                        state.update(status='TIMEOUT', exit_code=124)
                        break
                    if time.monotonic() >= next_heartbeat:
                        current_memory = memory_status()
                        if current_memory['status'] != 'PASS':
                            stop_owned_tree(process)
                            state.update(status='RESOURCE_LIMIT', exit_code=75,
                                         memory_at_stop=current_memory)
                            break
                        state.update(heartbeat_at=utc(), elapsed_seconds=round(elapsed, 3),
                                     memory=current_memory,
                                     stdout_bytes=(directory/'stdout.log').stat().st_size,
                                     stderr_bytes=(directory/'stderr.log').stat().st_size)
                        write_state(state_path, state)
                        print(json.dumps({'status': 'RUNNING', 'name': name,
                                          'elapsed_seconds': state['elapsed_seconds'],
                                          'receipt': str(state_path)}), flush=True)
                        next_heartbeat = time.monotonic() + heartbeat
                    time.sleep(min(0.2, timeout/10))
                else:
                    state.update(status='PASS' if process.returncode == 0 else 'FAILED',
                                 exit_code=process.returncode)
            except BaseException:
                stop_owned_tree(process)
                state.update(status='INTERRUPTED', exit_code=130, finished_at=utc())
                write_state(state_path, state)
                raise
    except OSError as exc:
        state.update(status='LAUNCH_FAILED', exit_code=127, error=type(exc).__name__)
    state.update(finished_at=utc(), elapsed_seconds=round(time.monotonic()-started, 3))
    write_state(state_path, state)
    print(json.dumps({'status': state['status'], 'exit_code': state['exit_code'],
                      'receipt': str(state_path)}), flush=True)
    return state


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('preflight')
    run = sub.add_parser('run')
    run.add_argument('--name', required=True)
    run.add_argument('--cwd', type=Path, required=True)
    run.add_argument('--timeout', type=float, default=900)
    run.add_argument('--heartbeat', type=float, default=10)
    run.add_argument('--log-root', type=Path, default=RUNTIME/'logs'/'execution_guard')
    run.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == 'preflight':
        result = preflight()
        print(json.dumps(result, indent=2))
        return 0 if result['status'] == 'PASS' else 78
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a child command is required after --')
    return run_command(command, name=args.name, cwd=args.cwd, log_root=args.log_root,
                       timeout=args.timeout, heartbeat=args.heartbeat)['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
