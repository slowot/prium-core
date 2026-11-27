#!/usr/bin/env python3
"""Synchronize vanilla Chromium source tree, toolchains, PGO profile, overlay, and patches."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT_DIR / 'UPSTREAM.lock.json'
PRUNING_LIST_PATH = ROOT_DIR / 'pruning.prium.list'

logging.basicConfig(level=logging.INFO, format='[sync_chromium] %(levelname)s: %(message)s')
LOG = logging.getLogger('sync_chromium')


def _make_ssl_context(disable_ssl_verification: bool) -> ssl.SSLContext | None:
    if not disable_ssl_verification:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _download_file(url: str, dest: Path, disable_ssl: bool = False) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        LOG.info('Using cached download %s', dest.name)
        return
    LOG.info('Downloading %s -> %s', url, dest.name)
    tmp_path = dest.with_suffix(dest.suffix + '.part')
    ctx = _make_ssl_context(disable_ssl)
    req = urllib.request.Request(url, headers={'User-Agent': 'prium-core-sync/1.0'})
    with urllib.request.urlopen(req, context=ctx, timeout=120) as resp, tmp_path.open('wb') as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)
    tmp_path.replace(dest)


def _compute_hash(file_path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with file_path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest().lower()


def _verify_hash(file_path: Path, expected: str, algorithm: str) -> None:
    actual = _compute_hash(file_path, algorithm)
    if actual != expected.lower():
        raise RuntimeError(
            f'{algorithm.upper()} mismatch for {file_path.name}: expected {expected}, got {actual}'
        )


def _parse_chromium_hashes(hashes_text: str) -> dict[str, tuple[str, str]]:
    """Parse Chromium official .hashes file into {algo: hex_digest}."""
    parsed: dict[str, str] = {}
    for raw_line in hashes_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            algo = parts[0].lower()
            digest = parts[1].lower()
            if algo in ('sha512', 'sha256', 'sha1', 'md5'):
                parsed[algo] = digest
    return parsed


def _find_7z(sevenz_arg: str | None) -> str:
    if sevenz_arg:
        return sevenz_arg
    for candidate in (
        shutil.which('7z'),
        shutil.which('7za'),
        r'C:\Program Files\7-Zip\7z.exe',
        r'C:\Program Files (x86)\7-Zip\7z.exe',
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise RuntimeError('7z executable not found in PATH or standard Program Files locations.')


def _move_stripped_tree(src_root: Path, dst_root: Path, strip_leading_dirs: str | None) -> None:
    effective_src = src_root / strip_leading_dirs if strip_leading_dirs else src_root
    if not effective_src.exists():
        raise RuntimeError(f'Expected stripped directory {effective_src} does not exist after unpack.')
    dst_root.mkdir(parents=True, exist_ok=True)
    for item in effective_src.iterdir():
        target = dst_root / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _extract_archive(
    archive_path: Path,
    output_dir: Path,
    strip_leading_dirs: str | None = None,
    extractor: str | None = None,
    sevenz_path: str | None = None,
) -> None:
    name_lower = archive_path.name.lower()
    use_7z = extractor == '7z' or name_lower.endswith(('.7z.exe', '.7z', '.exe'))

    staging_dir = output_dir.parent / f'.unpack_{archive_path.stem}_{os.getpid()}'
    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    try:
        if use_7z:
            sevenz = _find_7z(sevenz_path)
            staging_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [sevenz, 'x', str(archive_path), f'-o{staging_dir}', '-y'],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            _move_stripped_tree(staging_dir, output_dir, strip_leading_dirs)
        elif name_lower.endswith('.zip'):
            staging_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(staging_dir)
            _move_stripped_tree(staging_dir, output_dir, strip_leading_dirs)
        elif name_lower.endswith(('.tar.xz', '.tar.gz', '.tgz', '.tar.bz2')):
            staging_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, 'r:*') as tf:
                try:
                    tf.extractall(staging_dir, filter='data')
                except TypeError:
                    tf.extractall(staging_dir)
            _move_stripped_tree(staging_dir, output_dir, strip_leading_dirs)
        else:
            raise RuntimeError(f'Unsupported archive format: {archive_path.name}')
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def _sync_source_tarball(lock: dict, source_dir: Path, cache_dir: Path, disable_ssl: bool, sevenz_path: str | None) -> None:
    version = lock['version']
    tarball_name = f'chromium-{version}-lite.tar.xz'
    tarball_path = cache_dir / tarball_name
    hashes_path = cache_dir / f'{tarball_name}.hashes'

    _download_file(lock['source_hashes_url'], hashes_path, disable_ssl)
    hashes = _parse_chromium_hashes(hashes_path.read_text(encoding='utf-8'))
    if 'sha512' not in hashes and 'sha256' not in hashes:
        raise RuntimeError(f'No sha512/sha256 digest found in {hashes_path}')

    _download_file(lock['source_tarball'], tarball_path, disable_ssl)
    if 'sha512' in hashes:
        _verify_hash(tarball_path, hashes['sha512'], 'sha512')
    else:
        _verify_hash(tarball_path, hashes['sha256'], 'sha256')
    LOG.info('Verified %s checksum.', tarball_name)

    _extract_archive(
        tarball_path,
        source_dir,
        strip_leading_dirs=f'chromium-{version}',
        sevenz_path=sevenz_path,
    )


def _sync_source_git(lock: dict, source_dir: Path) -> None:
    version = lock['version']
    upstream_git = lock['upstream_git']
    LOG.info('Cloning Chromium %s from %s (shallow)...', version, upstream_git)
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['git', 'clone', '--depth', '1', '--branch', version, upstream_git, str(source_dir)],
        check=True,
    )
    gclient_bin = shutil.which('gclient')
    if gclient_bin:
        LOG.info('Running gclient sync for DEPS subrepositories...')
        subprocess.run([gclient_bin, 'sync', '--nohooks', '--no-history'], cwd=source_dir.parent, check=True)


def _prune_binaries(source_dir: Path) -> None:
    if not PRUNING_LIST_PATH.exists():
        return
    removed = 0
    for raw_line in PRUNING_LIST_PATH.read_text(encoding='utf-8').splitlines():
        rel = raw_line.strip()
        if not rel or rel.startswith('#'):
            continue
        target = source_dir / rel
        if target.is_file() or target.is_symlink():
            target.unlink()
            removed += 1
        elif target.is_dir():
            shutil.rmtree(target)
            removed += 1
    LOG.info('Pruned %d entry(ies) from %s.', removed, PRUNING_LIST_PATH.name)


def _sync_toolchains(lock: dict, source_dir: Path, cache_dir: Path, disable_ssl: bool, sevenz_path: str | None) -> None:
    toolchains = lock.get('toolchains', {})
    for name, spec in toolchains.items():
        dl_path = cache_dir / spec['download_filename']
        _download_file(spec['url'], dl_path, disable_ssl)
        if 'sha512' in spec:
            _verify_hash(dl_path, spec['sha512'], 'sha512')
        elif 'sha256' in spec:
            _verify_hash(dl_path, spec['sha256'], 'sha256')

        out_dir = source_dir / spec['output_path']
        if name in ('directx-headers', 'esbuild') and out_dir.exists():
            shutil.rmtree(out_dir)
        LOG.info('Unpacking toolchain %s -> %s', name, spec['output_path'])
        _extract_archive(
            dl_path,
            out_dir,
            strip_leading_dirs=spec.get('strip_leading_dirs'),
            extractor=spec.get('extractor'),
            sevenz_path=sevenz_path,
        )


def _populate_rust_toolchain(source_dir: Path) -> None:
    rust_dst = source_dir / 'third_party' / 'rust-toolchain'
    flag_file = rust_dst / 'INSTALLED_VERSION'
    if flag_file.exists():
        return

    src_x64 = source_dir / 'third_party' / 'rust-toolchain-x64'
    src_x86 = source_dir / 'third_party' / 'rust-toolchain-x86'
    src_arm = source_dir / 'third_party' / 'rust-toolchain-arm'
    host_is_64 = sys.maxsize > 2**32

    for rust_src in (src_x64, src_x86, src_arm):
        if not rust_src.exists():
            continue
        for sub in ('bin', 'lib'):
            if sub == 'bin' and (host_is_64 != (rust_src == src_x64)):
                continue
            target_dir = rust_dst / sub
            target_dir.mkdir(parents=True, exist_ok=True)
            for cp_src in rust_src.glob(f'*/{sub}/*'):
                cp_dst = target_dir / cp_src.name
                if cp_src.is_dir():
                    shutil.copytree(cp_src, cp_dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(cp_src, cp_dst)

    rustc_exe = src_x64 / 'rustc' / 'bin' / 'rustc.exe'
    if rustc_exe.exists():
        res = subprocess.run([str(rustc_exe), '--version'], capture_output=True, text=True, check=True)
        flag_file.write_text(res.stdout, encoding='utf-8')
        LOG.info('Populated third_party/rust-toolchain (%s)', res.stdout.strip())


def _sync_pgo_profile(lock: dict, source_dir: Path, disable_ssl: bool) -> None:
    pgo_txt = source_dir / 'chrome' / 'build' / 'win64.pgo.txt'
    if not pgo_txt.exists():
        LOG.warning('Missing %s; skipping PGO profile fetch.', pgo_txt)
        return
    prof_name = pgo_txt.read_text(encoding='utf-8').strip()
    if not prof_name:
        return
    pgo_dir = source_dir / 'chrome' / 'build' / 'pgo_profiles'
    pgo_dir.mkdir(parents=True, exist_ok=True)
    pgo_dest = pgo_dir / prof_name
    base_url = lock['pgo_url_base'].rstrip('/')
    _download_file(f'{base_url}/{prof_name}', pgo_dest, disable_ssl)


def _apply_overlay(source_dir: Path) -> None:
    overlay_dir = ROOT_DIR / 'chromium_src'
    if not overlay_dir.exists():
        LOG.info('No chromium_src/ overlay directory found; skipping overlay copy.')
        return
    copied = 0
    for src_file in overlay_dir.rglob('*'):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(overlay_dir)
        dst_file = source_dir / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        copied += 1
    LOG.info('Copied %d overlay file(s) from chromium_src/.', copied)


def _apply_patches(source_dir: Path) -> None:
    series_path = ROOT_DIR / 'patches' / 'series'
    if not series_path.exists():
        LOG.warning('Missing %s; skipping patch application.', series_path)
        return
    entries = [
        line.strip()
        for line in series_path.read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.strip().startswith('#')
    ]
    patch_exe = source_dir / 'third_party' / 'git' / 'usr' / 'bin' / 'patch.exe'
    use_git = (source_dir / '.git').exists()

    for entry in entries:
        patch_file = ROOT_DIR / 'patches' / entry
        if not patch_file.exists():
            raise FileNotFoundError(f'Patch listed in series not found: {patch_file}')
        LOG.info('Applying patch %s', entry)
        if use_git:
            subprocess.run(
                ['git', 'apply', '--whitespace=nowarn', '--ignore-whitespace', str(patch_file)],
                cwd=source_dir,
                check=True,
            )
        elif patch_exe.exists():
            with patch_file.open('rb') as pf:
                subprocess.run(
                    [str(patch_exe), '-p1', '--ignore-whitespace', '--no-backup-if-mismatch'],
                    cwd=source_dir,
                    stdin=pf,
                    check=True,
                )
        else:
            subprocess.run(
                ['git', 'apply', '--whitespace=nowarn', '--ignore-whitespace', str(patch_file)],
                cwd=source_dir,
                check=True,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--track', choices=('dev', 'release'), default='release')
    parser.add_argument('--method', choices=('auto', 'tarball', 'git'), default='auto')
    parser.add_argument('--source-dir', type=Path, default=ROOT_DIR / 'build' / 'src')
    parser.add_argument('--cache-dir', type=Path, default=ROOT_DIR / 'build' / 'download_cache')
    parser.add_argument('--7z-path', dest='sevenz_path', default=None)
    parser.add_argument('--disable-ssl-verification', action='store_true')
    parser.add_argument('--skip-patches', action='store_true')
    parser.add_argument('--skip-overlay', action='store_true')
    args = parser.parse_args()

    lock = json.loads(LOCK_PATH.read_text(encoding='utf-8'))
    source_dir: Path = args.source_dir.resolve()
    cache_dir: Path = args.cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not (source_dir / 'BUILD.gn').exists():
        if args.method == 'git':
            _sync_source_git(lock, source_dir)
        elif args.method == 'tarball':
            _sync_source_tarball(lock, source_dir, cache_dir, args.disable_ssl_verification, args.sevenz_path)
        else:
            try:
                _sync_source_tarball(lock, source_dir, cache_dir, args.disable_ssl_verification, args.sevenz_path)
            except (urllib.error.URLError, RuntimeError) as exc:
                LOG.warning('Tarball sync failed (%s); falling back to upstream git clone.', exc)
                if source_dir.exists():
                    shutil.rmtree(source_dir, ignore_errors=True)
                _sync_source_git(lock, source_dir)

    _prune_binaries(source_dir)
    _sync_toolchains(lock, source_dir, cache_dir, args.disable_ssl_verification, args.sevenz_path)
    _populate_rust_toolchain(source_dir)

    if args.track == 'release':
        _sync_pgo_profile(lock, source_dir, args.disable_ssl_verification)

    if not args.skip_overlay:
        _apply_overlay(source_dir)
    if not args.skip_patches:
        _apply_patches(source_dir)

    LOG.info('Chromium %s sync complete (%s track).', lock['version'], args.track)
    return 0


if __name__ == '__main__':
    sys.exit(main())
