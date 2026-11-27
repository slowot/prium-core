#!/usr/bin/env python3
"""Package Prium-Core Windows x64 runtime zip, separate PDB symbols zip, and SHA-256 manifest."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import sys
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT_DIR / 'UPSTREAM.lock.json'

logging.basicConfig(level=logging.INFO, format='[package_prium] %(levelname)s: %(message)s')
LOG = logging.getLogger('package_prium')

EXCLUDED_FILES = {
    Path('mini_installer.exe'),
    Path('mini_installer_exe_version.rc'),
    Path('setup.exe'),
    Path('chrome.packed.7z'),
}

SYMBOL_FILES = ('chrome.exe.pdb', 'chrome.dll.pdb', 'chrome_elf.dll.pdb')


def _read_version(source_dir: Path) -> str:
    chrome_version = source_dir / 'chrome' / 'VERSION'
    if chrome_version.exists():
        parts: dict[str, str] = {}
        for line in chrome_version.read_text(encoding='utf-8').splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                parts[k.strip()] = v.strip()
        if all(k in parts for k in ('MAJOR', 'MINOR', 'BUILD', 'PATCH')):
            return f"{parts['MAJOR']}.{parts['MINOR']}.{parts['BUILD']}.{parts['PATCH']}"
    return json.loads(LOCK_PATH.read_text(encoding='utf-8'))['version']


def _iter_filescfg(cfg_path: Path, build_outputs: Path, cpu_arch: str):
    resolved = build_outputs.resolve()
    exec_globals = {'__builtins__': None}
    exec(cfg_path.read_text(encoding='utf-8'), exec_globals)
    seen: set[Path] = set()
    for spec in exec_globals['FILES']:
        if 'official' not in spec.get('buildtype', ()):
            continue
        if 'arch' in spec and cpu_arch not in spec['arch']:
            continue
        for match in resolved.glob(spec['filename']):
            if match.suffix.lower() == '.pdb':
                continue
            rel = match.relative_to(resolved)
            if rel in EXCLUDED_FILES or rel in seen:
                continue
            seen.add(rel)
            yield rel


def _zip_files(items: list[tuple[Path, str]], output_path: Path, timestamp: int | None = None) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    zip_dt = datetime.datetime.fromtimestamp(timestamp).timetuple()[:6] if timestamp else None

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for src_path, arc_name in items:
            if src_path.is_dir():
                for sub in sorted(src_path.rglob('*')):
                    if sub.is_dir():
                        continue
                    sub_arc = f'{arc_name}/{sub.relative_to(src_path).as_posix()}'
                    _write_zip_entry(zf, sub, sub_arc, zip_dt)
            else:
                _write_zip_entry(zf, src_path, arc_name, zip_dt)

    h = hashlib.sha256()
    with output_path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest().lower()


def _write_zip_entry(zf: zipfile.ZipFile, src_path: Path, arc_name: str, zip_dt) -> None:
    if zip_dt:
        info = zipfile.ZipInfo.from_file(src_path, arc_name)
        info.date_time = zip_dt
        zf.writestr(info, src_path.read_bytes(), zipfile.ZIP_DEFLATED)
    else:
        zf.write(src_path, arc_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=ROOT_DIR / 'build' / 'src')
    parser.add_argument('--out-dir', default=r'out\Default')
    parser.add_argument('--dist-dir', type=Path, default=ROOT_DIR / 'build')
    parser.add_argument('--cpu-arch', choices=('64bit', '32bit', 'arm'), default='64bit')
    args = parser.parse_args()

    source_dir: Path = args.source_dir.resolve()
    build_outputs = source_dir / args.out_dir
    cfg_path = source_dir / 'chrome' / 'tools' / 'build' / 'win' / 'FILES.cfg'
    if not cfg_path.exists():
        raise FileNotFoundError(f'Missing FILES.cfg at {cfg_path}')

    version = _read_version(source_dir)
    timestamp = None
    lastchange = source_dir / 'build' / 'util' / 'LASTCHANGE.committime'
    if lastchange.exists():
        timestamp = int(lastchange.read_text(encoding='utf-8').strip())

    runtime_stem = f'ant-chromium_{version}_windows_x64'
    runtime_zip = args.dist_dir / f'{runtime_stem}.zip'
    runtime_items = [
        (build_outputs / rel, f'{runtime_stem}/{rel.as_posix()}')
        for rel in _iter_filescfg(cfg_path, build_outputs, args.cpu_arch)
    ]
    runtime_sha = _zip_files(runtime_items, runtime_zip, timestamp)
    LOG.info('Created %s (%d entries, sha256=%s)', runtime_zip.name, len(runtime_items), runtime_sha)

    manifest_lines = [f'{runtime_sha}  {runtime_zip.name}']

    pdb_items = [
        (build_outputs / pdb, f'prium-core-symbols_{version}_windows_x64/{pdb}')
        for pdb in SYMBOL_FILES
        if (build_outputs / pdb).exists()
    ]
    if pdb_items:
        sym_zip = args.dist_dir / f'prium-core-symbols_{version}_windows_x64.zip'
        sym_sha = _zip_files(pdb_items, sym_zip, timestamp)
        manifest_lines.append(f'{sym_sha}  {sym_zip.name}')
        LOG.info('Created %s (%d PDBs, sha256=%s)', sym_zip.name, len(pdb_items), sym_sha)

    sha_manifest = args.dist_dir / f'{runtime_stem}.zip.sha256'
    sha_manifest.write_text('\n'.join(manifest_lines) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
