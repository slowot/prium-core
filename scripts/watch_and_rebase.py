#!/usr/bin/env python3
"""Poll ChromiumDash Stable Windows releases, inspect DEPS/win64.pgo.txt, and rebase patches with --3way + .anchor.json."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CHROMIUM_DASH_URL = 'https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Windows&num=5'

logging.basicConfig(level=logging.INFO, format='[watch_and_rebase] %(levelname)s: %(message)s')
LOG = logging.getLogger('watch_and_rebase')


def parse_chromium_dash_stable(payload: list[dict]) -> str | None:
    """Return the newest Stable Windows version string from ChromiumDash JSON."""
    for entry in payload:
        ver = str(entry.get('version', '')).strip()
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', ver):
            return ver
    return None


def parse_deps_metadata(deps_text: str) -> dict[str, str]:
    """Extract key subrepo pins from Chromium DEPS."""
    out: dict[str, str] = {}
    for key in ('devtools_frontend_revision', 'dawn_revision', 'gn_version'):
        m = re.search(rf"'{key}'\s*:\s*'([^']+)'", deps_text)
        if m:
            out[key] = m.group(1)
    return out


def fetch_latest_stable_windows() -> str:
    req = urllib.request.Request(CHROMIUM_DASH_URL, headers={'User-Agent': 'prium-core-rebase/1.0'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    ver = parse_chromium_dash_stable(data)
    if not ver:
        raise RuntimeError('No valid Stable Windows release found in ChromiumDash response.')
    return ver


def fetch_upstream_file(version: str, rel_path: str) -> str:
    url = f'https://chromium.googlesource.com/chromium/src/+/refs/tags/{version}/{rel_path}?format=TEXT'
    req = urllib.request.Request(url, headers={'User-Agent': 'prium-core-rebase/1.0'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return base64.b64decode(resp.read()).decode('utf-8')


def relocate_hunk_by_anchor(target_text: str, patch_text: str, anchors: list[dict]) -> str | None:
    """Relocate unified diff hunk start lines using semantic regex anchors."""
    target_lines = target_text.splitlines()
    patch_lines = patch_text.splitlines()
    updated = False
    new_patch: list[str] = []
    hunk_idx = 0
    for line in patch_lines:
        m = re.match(r'^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@(.*)$', line)
        if m and hunk_idx < len(anchors):
            anchor_re = anchors[hunk_idx].get('regex', '')
            offset_from_anchor = int(anchors[hunk_idx].get('offset', 0))
            if anchor_re:
                for idx, tline in enumerate(target_lines, start=1):
                    if re.search(anchor_re, tline):
                        new_start = max(1, idx + offset_from_anchor)
                        old_cnt = m.group(2) or ''
                        new_cnt = m.group(4) or ''
                        tail = m.group(5)
                        line = f'@@ -{new_start}{old_cnt} +{new_start}{new_cnt} @@{tail}'
                        updated = True
                        break
            hunk_idx += 1
        new_patch.append(line)
    if not updated:
        return None
    return '\n'.join(new_patch) + '\n'


def apply_patch_with_rebase(repo_dir: Path, patch_path: Path, use_3way: bool = True) -> tuple[bool, str]:
    """Attempt git apply (--3way fallback + .anchor.json semantic relocation)."""
    base_cmd = ['git', 'apply', '--ignore-whitespace', '--whitespace=nowarn']
    check_res = subprocess.run(base_cmd + ['--check', str(patch_path)], cwd=repo_dir, capture_output=True, text=True)
    if check_res.returncode == 0:
        subprocess.run(base_cmd + [str(patch_path)], cwd=repo_dir, check=True)
        return True, 'clean'

    if use_3way:
        res_3way = subprocess.run(base_cmd + ['--3way', str(patch_path)], cwd=repo_dir, capture_output=True, text=True)
        if res_3way.returncode == 0:
            return True, '3way'

    anchor_path = patch_path.with_suffix('.anchor.json')
    if anchor_path.exists():
        anchor_spec = json.loads(anchor_path.read_text(encoding='utf-8'))
        rel_file = anchor_spec.get('file')
        anchors = anchor_spec.get('anchors', [])
        if rel_file and (repo_dir / rel_file).exists():
            relocated = relocate_hunk_by_anchor(
                (repo_dir / rel_file).read_text(encoding='utf-8'),
                patch_path.read_text(encoding='utf-8'),
                anchors,
            )
            if relocated:
                tmp_patch = patch_path.with_suffix('.relocated.patch')
                tmp_patch.write_text(relocated, encoding='utf-8')
                try:
                    cmd = base_cmd + (['--3way'] if use_3way else []) + [str(tmp_patch)]
                    res_reloc = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
                    if res_reloc.returncode == 0:
                        return True, 'anchor-relocated'
                finally:
                    tmp_patch.unlink(missing_ok=True)

    return False, check_res.stderr.strip()


def run_self_test() -> int:
    """Offline self-test for ChromiumDash parsing, DEPS extraction, --3way, and .anchor.json rebase."""
    sample_dash = [
        {'version': '150.0.7871.190', 'channel': 'Stable', 'platform': 'Windows'},
        {'version': '150.0.7871.186', 'channel': 'Stable', 'platform': 'Windows'},
    ]
    assert parse_chromium_dash_stable(sample_dash) == '150.0.7871.190'

    sample_deps = (
        "  'devtools_frontend_revision': 'ec97cf3bbeea2cb623fbf97c4e3f22f5acb4d568',\n"
        "  'gn_version': 'git_revision:3357c4f51b1a9e676378c695dd9c7e9911c35ee6',\n"
    )
    meta = parse_deps_metadata(sample_deps)
    assert meta['devtools_frontend_revision'] == 'ec97cf3bbeea2cb623fbf97c4e3f22f5acb4d568'
    assert meta['gn_version'] == 'git_revision:3357c4f51b1a9e676378c695dd9c7e9911c35ee6'

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo_dir = tmp_path / 'repo'
        repo_dir.mkdir()
        subprocess.run(['git', 'init'], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['git', 'config', 'user.email', 'test@prium.local'], cwd=repo_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Prium Test'], cwd=repo_dir, check=True)
        subprocess.run(['git', 'config', 'core.autocrlf', 'false'], cwd=repo_dir, check=True)

        src_file = repo_dir / 'foo.cc'
        src_file.write_text('int Header() { return 0; }\nint TargetFunc() {\n  return 1;\n}\n', encoding='utf-8')
        subprocess.run(['git', 'add', 'foo.cc'], cwd=repo_dir, check=True)
        subprocess.run(['git', 'commit', '-m', 'base'], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL)

        # Shift lines down by 20 lines and alter adjacent comment so plain apply fails and --3way / anchor runs
        padding = '\n'.join(f'// line {i}' for i in range(1, 21)) + '\n'
        src_file.write_text(
            padding + 'int Header() { return 0; }\nint TargetFunc() {\n  return 1;\n}\n',
            encoding='utf-8',
        )
        subprocess.run(['git', 'add', 'foo.cc'], cwd=repo_dir, check=True)
        subprocess.run(['git', 'commit', '-m', 'upstream-shift'], cwd=repo_dir, check=True, stdout=subprocess.DEVNULL)

        patch_file = tmp_path / 'sample.patch'
        patch_file.write_text(
            '--- a/foo.cc\n'
            '+++ b/foo.cc\n'
            '@@ -2,3 +2,3 @@\n'
            ' int TargetFunc() {\n'
            '-  return 1;\n'
            '+  return 42;\n'
            ' }\n',
            encoding='utf-8',
        )
        anchor_file = tmp_path / 'sample.anchor.json'
        anchor_file.write_text(
            json.dumps({'file': 'foo.cc', 'anchors': [{'regex': r'int TargetFunc\(\)', 'offset': 0}]}),
            encoding='utf-8',
        )

        # Verify anchor relocation directly on the shifted file
        relocated = relocate_hunk_by_anchor(
            src_file.read_text(encoding='utf-8'),
            patch_file.read_text(encoding='utf-8'),
            [{'regex': r'int TargetFunc\(\)', 'offset': 0}],
        )
        assert relocated and '@@ -22,3 +22,3 @@' in relocated, f'Unexpected relocated patch: {relocated}'

        ok, mode = apply_patch_with_rebase(repo_dir, patch_file, use_3way=True)
        assert ok and mode in ('clean', '3way', 'anchor-relocated'), f'Rebase failed: {mode}'
        assert 'return 42;' in src_file.read_text(encoding='utf-8')

    print('watch_and_rebase.py self-test PASS')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--3way', dest='use_3way', action='store_true', default=True)
    parser.add_argument('--create-pr', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    lock_path = ROOT_DIR / 'UPSTREAM.lock.json'
    current_ver = json.loads(lock_path.read_text(encoding='utf-8'))['version'] if lock_path.exists() else ''
    latest_ver = fetch_latest_stable_windows()
    LOG.info('Current locked=%s | ChromiumDash Stable Windows=%s', current_ver, latest_ver)

    if current_ver == latest_ver:
        LOG.info('Already up to date at %s.', current_ver)
        return 0

    deps_text = fetch_upstream_file(latest_ver, 'DEPS')
    pgo_name = fetch_upstream_file(latest_ver, 'chrome/build/win64.pgo.txt').strip()
    meta = parse_deps_metadata(deps_text)
    LOG.info('New release %s: DEPS=%s win64.pgo=%s', latest_ver, meta, pgo_name)

    if args.check_only:
        return 0

    if args.create_pr:
        branch = f'chore/rebase-{latest_ver}'
        subprocess.run(['git', 'checkout', '-b', branch], cwd=ROOT_DIR, check=True)
        LOG.info('Created rebase branch %s (manual patch verification required before PR push).', branch)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
