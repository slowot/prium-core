#!/usr/bin/env python3
"""Build Prium-Core Chromium 150 Win64 with dual-track GN flags, sccache, and pre-compile gates."""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BUILD_TIMEOUT_EXIT_CODE = 124
STAGE_TIMEOUT_SECONDS = 3.5 * 60 * 60

logging.basicConfig(level=logging.INFO, format='[build_prium] %(levelname)s: %(message)s')
LOG = logging.getLogger('build_prium')

RESILIENT_NODE_PY = r"""#!/usr/bin/env python
# Copyright 2017 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from os import path as os_path
import os
import platform
import shutil
import subprocess
import sys
import time


def GetBinaryPath():
  if platform.machine() == 'arm64':
    darwin_path = 'mac_arm64'
    darwin_name = 'node-darwin-arm64'
  else:
    darwin_path = 'mac'
    darwin_name = 'node-darwin-x64'
  return os_path.join(os_path.dirname(__file__), *{
    'Darwin': (darwin_path, darwin_name, 'bin', 'node'),
    'Linux': ('linux', 'node-linux-x64', 'bin', 'node'),
    'Windows': ('win', 'node.exe'),
  }[platform.system()])


def _RunWithCommand(cmd):
  process = subprocess.Popen(
      cmd, cwd=os.getcwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
      universal_newlines=True, encoding='utf-8')
  stdout, stderr = process.communicate()
  return process.returncode, stdout, stderr


def RunNodeRaw(cmd_parts, stdout=None):
  return _RunWithCommand([GetBinaryPath()] + cmd_parts)


def RunNode(cmd_parts, stdout=None):
  bundled = GetBinaryPath()
  fallback = shutil.which('node') or bundled
  attempts = [
      (0, [bundled] + cmd_parts),
      (3, [bundled] + cmd_parts),
      (5, [bundled, '--liftoff-only'] + cmd_parts),
      (8, [fallback, '--liftoff-only'] + cmd_parts),
  ]
  last_cmd = attempts[0][1]
  code = 1
  out = ''
  err = ''
  for idx, (delay, cmd) in enumerate(attempts):
    if delay > 0:
      time.sleep(delay)
    last_cmd = cmd
    code, out, err = _RunWithCommand(cmd)
    if code == 0:
      return out
    sys.stderr.write(
        'node.py attempt %d/%d exited %d for %s\n'
        % (idx + 1, len(attempts), code, cmd[0]))

  errs = []
  if err:
    errs.append('stderr:\n' + err)
  if out:
    errs.append('stdout:\n' + out)
  errs.append('exit=%d' % code)
  raise RuntimeError('Command \'%s\' failed\n%s' % (
      ' '.join(last_cmd), '\n'.join(errs)))


if __name__ == '__main__':
  RunNode(sys.argv[1:])
"""


def _get_vcvars_path(arch: str = '64') -> Path:
    vswhere = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / (
        r'Microsoft Visual Studio\Installer\vswhere.exe'
    )
    res = subprocess.run(
        [str(vswhere), '-products', '*', '-prerelease', '-latest', '-property', 'installationPath'],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    vcvars = Path(res.stdout.strip()) / 'VC' / 'Auxiliary' / 'Build' / f'vcvars{arch}.bat'
    if not vcvars.exists():
        raise RuntimeError(f'Missing vcvars batch script: {vcvars}')
    return vcvars


def _run_vc_command(args: list[str], cwd: Path) -> None:
    cmd_input = [
        f'call "{_get_vcvars_path()}" >nul',
        'set DEPOT_TOOLS_WIN_TOOLCHAIN=0',
        ' '.join(f'"{a}"' for a in args),
        'exit',
        '',
    ]
    subprocess.run(
        ('cmd.exe', '/k'),
        input='\n'.join(cmd_input),
        cwd=cwd,
        check=True,
        encoding='utf-8',
    )


def _run_vc_command_timeout(args: list[str], cwd: Path, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise KeyboardInterrupt

        attempt_args = list(args)
        if attempt == max_attempts:
            for idx in range(len(attempt_args) - 1):
                if attempt_args[idx] == '-j' and attempt_args[idx + 1] == '4':
                    attempt_args[idx + 1] = '2'
                    break

        cmd_input = [
            f'call "{_get_vcvars_path()}" >nul',
            'set DEPOT_TOOLS_WIN_TOOLCHAIN=0',
            ' '.join(f'"{a}"' for a in attempt_args),
            'exit',
            '',
        ]
        with subprocess.Popen(
            ('cmd.exe', '/k'),
            cwd=cwd,
            encoding='utf-8',
            stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        ) as proc:
            assert proc.stdin is not None
            proc.stdin.write('\n'.join(cmd_input))
            proc.stdin.close()
            try:
                proc.wait(remaining)
                if proc.returncode == 0:
                    return
                if attempt < max_attempts and (deadline - time.monotonic()) > 120:
                    backoff = 5 * attempt
                    LOG.warning(
                        'Ninja exited %d on attempt %d/%d; retrying in %ds...',
                        proc.returncode,
                        attempt,
                        max_attempts,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f'Ninja build failed with exit code {proc.returncode}')
            except subprocess.TimeoutExpired:
                for _ in range(3):
                    ctypes.windll.kernel32.GenerateConsoleCtrlEvent(1, proc.pid)
                    time.sleep(1)
                try:
                    proc.wait(10)
                except Exception:
                    proc.kill()
                raise KeyboardInterrupt


def parse_gn_file(path: Path) -> dict[str, str]:
    """Parse key = value pairs from a GN args file, preserving last assignment."""
    entries: dict[str, str] = {}
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.split('#', 1)[0].strip()
        if not line:
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', line)
        if not m:
            raise ValueError(f'Invalid GN assignment in {path}: {raw_line}')
        entries[m.group(1)] = m.group(2).strip()
    return entries


def build_merged_args(track: str, source_dir: Path) -> str:
    """Merge and deduplicate prium-base.gn + prium-<track>.gn + dynamic sccache/pgo args."""
    base_flags = parse_gn_file(ROOT_DIR / 'flags' / 'prium-base.gn')
    track_flags = parse_gn_file(ROOT_DIR / 'flags' / f'prium-{track}.gn')
    merged: dict[str, str] = {}
    merged.update(base_flags)
    merged.update(track_flags)

    if os.environ.get('SCCACHE_BUCKET', '').strip() and shutil.which('sccache'):
        merged['cc_wrapper'] = '"sccache"'

    if track == 'release':
        pgo_txt = source_dir / 'chrome' / 'build' / 'win64.pgo.txt'
        if pgo_txt.exists():
            prof_name = pgo_txt.read_text(encoding='utf-8').strip()
            prof_file = source_dir / 'chrome' / 'build' / 'pgo_profiles' / prof_name
            if prof_name and prof_file.exists():
                merged['pgo_data_path'] = f'"//chrome/build/pgo_profiles/{prof_name}"'
            else:
                raise RuntimeError(f'Missing release PGO profile: {prof_file}')

    lines = [f'{k} = {v}' for k, v in merged.items()]
    return '\n'.join(lines) + '\n'


def _install_resilient_node_wrapper(source_dir: Path) -> None:
    node_py = source_dir / 'third_party' / 'node' / 'node.py'
    if not node_py.parent.exists():
        return
    node_py.write_text(RESILIENT_NODE_PY, encoding='utf-8')
    LOG.info('Installed resilient node.py wrapper at %s', node_py)


def _discover_gate_targets(source_dir: Path, out_dir_rel: str) -> tuple[list[str], list[str]]:
    wanted_base_objs = {
        'ant_shm_config.obj',
        'ant_blackbox_logger.obj',
        'prium_net_guard.obj',
        'prium_vault.obj',
    }
    wanted_patched_objs = {
        'chrome_main_delegate.obj',
        'navigator.obj',
        'navigator_ua_data.obj',
        'navigator_concurrent_hardware.obj',
        'navigator_device_memory.obj',
        'element.obj',
        'html_canvas_element.obj',
        'offscreen_canvas.obj',
        'base_rendering_context_2d.obj',
        'webgl_rendering_context_base.obj',
        'audio_buffer.obj',
        'rtc_peer_connection.obj',
        'url_loader.obj',
        'entropy_provider.obj',
        'variations_http_headers.obj',
        'variations_service.obj',
        'http_network_session.obj',
        'ssl_client_socket_impl.obj',
        'renderer_main.obj',
        'font_cache_skia_win.obj',
        'dpapi_key_provider.obj',
        'os_crypt_win.obj',
        'screen_win.obj',
        'battery_status_manager_win.obj',
        'gpu_adapter.obj',
    }
    ninja_exe = source_dir / 'third_party' / 'ninja' / 'ninja.exe'
    res = subprocess.run(
        [str(ninja_exe), '-C', out_dir_rel, '-t', 'targets', 'all'],
        cwd=source_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return [], []

    base_targets: list[str] = []
    patched_targets: list[str] = []
    for line in res.stdout.splitlines():
        target = line.split(':', 1)[0].strip()
        if not target.endswith('.obj'):
            continue
        if '_for_' in target or 'unittest' in target or 'browsertest' in target:
            continue
        filename = target.rsplit('/', 1)[-1]
        if target.startswith('obj/base/') and filename in wanted_base_objs:
            base_targets.append(target)
        elif filename in wanted_patched_objs and target.startswith((
            'obj/chrome/app/',
            'obj/components/',
            'obj/content/renderer/',
            'obj/net/',
            'obj/services/network/',
            'obj/services/device/',
            'obj/ui/display/',
            'obj/third_party/blink/renderer/core/',
            'obj/third_party/blink/renderer/modules/',
            'obj/third_party/blink/renderer/platform/',
        )):
            patched_targets.append(target)

    return sorted(set(base_targets)), sorted(set(patched_targets))


def _ensure_tmp_dirs() -> None:
    for var in ('TMP', 'TEMP'):
        val = os.environ.get(var)
        if val:
            Path(val).mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--track', choices=('dev', 'release'), default='release')
    parser.add_argument('--ci', action='store_true')
    parser.add_argument('-j', '--thread-count', type=int, default=None)
    parser.add_argument('--source-dir', type=Path, default=ROOT_DIR / 'build' / 'src')
    parser.add_argument('--out-dir', default=r'out\Default')
    args = parser.parse_args()

    source_dir: Path = args.source_dir.resolve()
    out_dir_path = source_dir / args.out_dir
    _ensure_tmp_dirs()

    if not args.ci or not (out_dir_path / 'args.gn').exists():
        out_dir_path.mkdir(parents=True, exist_ok=True)
        args_gn_content = build_merged_args(args.track, source_dir)
        (out_dir_path / 'args.gn').write_text(args_gn_content, encoding='utf-8')
        LOG.info('Wrote deduplicated args.gn for track=%s', args.track)

    _install_resilient_node_wrapper(source_dir)

    gn_exe = out_dir_path / 'gn.exe'
    if not args.ci or not gn_exe.exists():
        _run_vc_command(
            [sys.executable, r'tools\gn\bootstrap\bootstrap.py', '-o', f'{args.out_dir}\\gn.exe', '--skip-generate-buildfiles'],
            cwd=source_dir,
        )
        _run_vc_command([f'{args.out_dir}\\gn.exe', 'gen', args.out_dir, '--fail-on-unused-args'], cwd=source_dir)

    bindgen_exe = source_dir / 'third_party' / 'rust-toolchain' / 'bin' / 'bindgen.exe'
    if not args.ci or not bindgen_exe.exists():
        _run_vc_command([sys.executable, r'tools\rust\build_bindgen.py', '--skip-test'], cwd=source_dir)

    base_objs, patched_objs = _discover_gate_targets(source_dir, args.out_dir)
    if base_objs:
        _run_vc_command([r'third_party\ninja\ninja.exe', '-j', '4', '-C', args.out_dir, *base_objs], cwd=source_dir)
    _run_vc_command(
        [
            r'third_party\ninja\ninja.exe',
            '-j',
            '2',
            '-C',
            args.out_dir,
            'gen/third_party/devtools-frontend/src/front_end/entrypoints/main/main-meta.js',
        ],
        cwd=source_dir,
    )
    if patched_objs:
        _run_vc_command([r'third_party\ninja\ninja.exe', '-j', '4', '-C', args.out_dir, *patched_objs], cwd=source_dir)

    ninja_cmd = [r'third_party\ninja\ninja.exe']
    if args.thread_count is not None:
        ninja_cmd.extend(['-j', str(args.thread_count)])
    ninja_cmd.extend(['-C', args.out_dir, 'chrome'])

    if args.ci:
        try:
            _run_vc_command_timeout(ninja_cmd, cwd=source_dir, timeout_seconds=STAGE_TIMEOUT_SECONDS)
        except KeyboardInterrupt:
            LOG.warning('Build stage hit 3.5h timeout; exiting with %d for stage resume.', BUILD_TIMEOUT_EXIT_CODE)
            return BUILD_TIMEOUT_EXIT_CODE
        subprocess.run(
            [sys.executable, str(ROOT_DIR / 'scripts' / 'package_prium.py'), '--source-dir', str(source_dir), '--out-dir', args.out_dir],
            check=True,
        )
    else:
        _run_vc_command(ninja_cmd, cwd=source_dir)

    return 0


if __name__ == '__main__':
    sys.exit(main())
