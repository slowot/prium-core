#!/usr/bin/env python3
"""Validate Prium-Core UPSTREAM.lock.json, GN flags, pruning list, and patches."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

FORBIDDEN_GN_KEYS = {
    'is_cfi',
    'rustc_wrapper',
    'v8_drumbrake_bounds_checks',
    'enable_mdns',
    'enable_service_discovery',
    'enable_reporting',
}

FORBIDDEN_PATTERNS = (
    'qjz9zk',
    'domain_regex',
    'domain_substitution',
    'windows-disable-encryption',
    'windows-fix-unsupported-llvm-flags',
    'ungoogled_platform_flag_entries',
    'FingerprintingCanvasImageDataNoiseEnabled',
    'FingerprintingClientRectsNoiseEnabled',
    'kSpoofWebGLInfo',
)


def _parse_gn(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', line)
        if not m:
            raise AssertionError(f'Invalid GN syntax in {path}: {raw}')
        key, val = m.group(1), m.group(2).strip()
        if key in result:
            raise AssertionError(f'Duplicate GN key {key} inside {path}')
        result[key] = val
    return result


def validate_lock() -> None:
    lock_path = ROOT_DIR / 'UPSTREAM.lock.json'
    version_path = ROOT_DIR / 'VERSION'
    assert lock_path.exists(), f'Missing {lock_path}'
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    assert lock['version'] == version_path.read_text(encoding='utf-8').strip(), 'VERSION mismatch with UPSTREAM.lock.json'
    assert lock['upstream_git'] == 'https://chromium.googlesource.com/chromium/src.git'
    assert lock['source_tarball'].endswith(f"chromium-{lock['version']}-lite.tar.xz")
    assert lock['source_hashes_url'].endswith('.hashes')
    assert lock['pgo_url_base'] == 'https://storage.googleapis.com/chromium-optimization-profiles/pgo_profiles'

    toolchains = lock['toolchains']
    assert toolchains['llvm']['version'] == '23.1.0'
    assert len(toolchains['llvm']['sha512']) == 128
    assert lock['rust_version'] == '2026-04-10'
    for key in ('rust-x64', 'rust-x86', 'rust-arm', 'rust-windows-rs', 'nodejs', 'esbuild', 'ninja', 'gperf', 'bison-bin'):
        assert key in toolchains, f'Missing toolchain {key}'
        assert len(toolchains[key]['sha512']) == 128
    assert len(toolchains['git']['sha256']) == 64
    for key in ('directx-headers', 'webauthn'):
        assert key in toolchains, f'Missing toolchain {key}'


def validate_gn_flags() -> None:
    base = _parse_gn(ROOT_DIR / 'flags' / 'prium-base.gn')
    dev = _parse_gn(ROOT_DIR / 'flags' / 'prium-dev.gn')
    rel = _parse_gn(ROOT_DIR / 'flags' / 'prium-release.gn')

    for name, table in (('prium-base.gn', base), ('prium-dev.gn', dev), ('prium-release.gn', rel)):
        bad = FORBIDDEN_GN_KEYS.intersection(table.keys())
        assert not bad, f'Forbidden GN args in {name}: {sorted(bad)}'

    assert not set(base).intersection(dev), f'Overlap base/dev: {set(base) & set(dev)}'
    assert not set(base).intersection(rel), f'Overlap base/release: {set(base) & set(rel)}'

    assert base['target_os'] == '"win"'
    assert base['target_cpu'] == '"x64"'
    assert base['safe_browsing_mode'] == '0'
    assert dev['chrome_pgo_phase'] == '0'
    assert dev['use_thin_lto'] == 'false'
    assert rel['chrome_pgo_phase'] == '2'
    assert rel['use_thin_lto'] == 'true'


def validate_unified_diff(patch_path: Path) -> None:
    lines = patch_path.read_text(encoding='utf-8').splitlines()
    file_headers = 0
    hunks = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('--- ') and (i + 1) < len(lines) and lines[i + 1].startswith('+++ '):
            file_headers += 1
            i += 2
            continue
        m = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
        if m:
            exp_old = int(m.group(2)) if m.group(2) is not None else 1
            exp_new = int(m.group(4)) if m.group(4) is not None else 1
            act_old = 0
            act_new = 0
            j = i + 1
            while j < len(lines):
                hl = lines[j]
                if hl.startswith(('@@ ', '--- ', '+++ ', 'diff --git ')):
                    break
                if hl.startswith('-'):
                    act_old += 1
                elif hl.startswith('+'):
                    act_new += 1
                elif hl.startswith(' ') or hl == '':
                    act_old += 1
                    act_new += 1
                elif hl.startswith(r'\ No newline at end of file'):
                    pass
                else:
                    break
                j += 1
            assert act_old == exp_old and act_new == exp_new, (
                f'Hunk count mismatch in {patch_path.name}:{i + 1}: '
                f'expected -{exp_old},+{exp_new} got -{act_old},+{act_new}'
            )
            hunks += 1
            i = j
            continue
        i += 1
    assert file_headers > 0 and hunks > 0, f'No valid hunks in {patch_path}'


def validate_repo_hygiene() -> None:
    pruning = ROOT_DIR / 'pruning.prium.list'
    assert pruning.exists(), 'Missing pruning.prium.list'
    pruning_lines = [l.strip() for l in pruning.read_text(encoding='utf-8').splitlines() if l.strip() and not l.strip().startswith('#')]
    assert len(pruning_lines) <= 25, f'pruning.prium.list too large ({len(pruning_lines)} lines)'
    assert 'chrome/browser/resources/media/mei_preload/preloaded_data.pb' in pruning_lines

    for legacy in ('domain_regex.list', 'domain_substitution.list', 'pruning.list', 'downloads.ini'):
        assert not (ROOT_DIR / legacy).exists(), f'Legacy ungoogled artifact must not exist: {legacy}'

    for patch_file in sorted((ROOT_DIR / 'patches').rglob('*.patch')):
        validate_unified_diff(patch_file)

    for file_path in ROOT_DIR.rglob('*'):
        if not file_path.is_file():
            continue
        if any(part in file_path.parts for part in ('.git', '__pycache__', 'build', 'dist', 'node_modules')):
            continue
        if file_path.name == 'validate_prium.py':
            continue
        if file_path.suffix in ('.png', '.zip', '.exe', '.pdb', '.pyc'):
            continue
        txt = file_path.read_text(encoding='utf-8', errors='ignore')
        for pat in FORBIDDEN_PATTERNS:
            assert pat not in txt, f'Forbidden pattern {pat!r} found in {file_path.relative_to(ROOT_DIR)}'


def main() -> int:
    validate_lock()
    validate_gn_flags()
    validate_repo_hygiene()
    print('validate_prium.py: ALL CHECKS PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
