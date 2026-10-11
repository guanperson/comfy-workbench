"""Create clean source and Windows release ZIPs without including runtime data.

Run after build.py from the same Python environment used to build the EXE:
    python tools/package_release.py --output ../comfy-workbench-release
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import __version__

TOP_LEVEL = (
    'main.py', 'build.py', 'README.md', 'LICENSE', 'ROADMAP.md',
    'THIRD_PARTY_NOTICES.md', '.gitignore', 'requirements.txt',
    'requirements-dev.txt', 'requirements-wd14-cpu.txt', 'requirements-wd14-gpu.txt',
    'selftest.py', 'uitest.py', 'test_preprocess.py', 'test_preprocess_ui.py',
    'test_trash_pairs.py', 'verify_packed_ui.py',
)
COMPONENTS = (
    'PySide6-Essentials', 'shiboken6', 'Pillow', 'numpy', 'onnxruntime-gpu',
    'nvidia-cublas-cu12', 'nvidia-cuda-runtime-cu12', 'nvidia-cudnn-cu12',
    'nvidia-cufft-cu12', 'nvidia-cuda-nvrtc-cu12', 'nvidia-nvjitlink-cu12',
    'PyInstaller', 'packaging', 'psutil', 'charset-normalizer', 'coloredlogs',
    'humanfriendly', 'flatbuffers', 'protobuf', 'sympy', 'mpmath', 'typing_extensions',
)


def source_files() -> list[Path]:
    result = [ROOT / name for name in TOP_LEVEL]
    for directory, suffixes in [('app', {'.py'}), ('tools', {'.py'}), ('.github', {'.yml', '.yaml'})]:
        result.extend(p for p in (ROOT / directory).rglob('*')
                      if p.is_file() and p.suffix in suffixes and '__pycache__' not in p.parts)
    if any(not p.is_file() for p in result):
        raise RuntimeError('Missing source release file')
    return sorted(result)


def collect_licenses(destination: Path) -> None:
    versions = {'Python': sys.version.split()[0], 'application': __version__}
    for name in COMPONENTS:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        versions[name] = distribution.version
        for entry in distribution.files or []:
            if not any(token in str(entry).lower() for token in ('license', 'copying', 'notice')):
                continue
            actual = Path(distribution.locate_file(entry))
            if not actual.is_file():
                continue
            # Keep the package path so multiple license files never overwrite.
            relative = Path(*[part for part in entry.parts if part not in ('..', '.', '/')])
            target = destination / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(actual, target)
    texts = {
        'LGPL-3.0.txt': 'https://raw.githubusercontent.com/spdx/license-list-data/main/text/LGPL-3.0-only.txt',
        'GPL-3.0.txt': 'https://raw.githubusercontent.com/spdx/license-list-data/main/text/GPL-3.0-only.txt',
        'Python-LICENSE.txt': f'https://raw.githubusercontent.com/python/cpython/v{versions["Python"]}/LICENSE',
    }
    destination.mkdir(parents=True, exist_ok=True)
    for filename, url in texts.items():
        request = urllib.request.Request(url, headers={'User-Agent': 'ComfyUI-Workbench-release/0.2'})
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read()
        if len(data) < 500 or b'<html' in data[:500].lower():
            raise RuntimeError(f'Invalid license response: {url}')
        (destination / filename).write_bytes(data)
    (destination / 'COMPONENT_VERSIONS.json').write_text(
        json.dumps(versions, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
    )


def zip_files(target: Path, files: list[tuple[Path, str]]) -> None:
    if target.exists():
        raise RuntimeError(f'Refusing to overwrite existing archive: {target}')
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, name in files:
            archive.write(path, name)
    with zipfile.ZipFile(target) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f'Archive CRC failed: {bad}')
    print(f'ZIP_OK {target.name} {target.stat().st_size / 1024 / 1024:.1f} MiB', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT.parent / 'comfy-workbench-release')
    parser.add_argument('--resume', action='store_true', help='Resume license/archive generation after an interrupted first run')
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        raise RuntimeError('Release output must be outside the source repository')
    packed = ROOT / 'dist' / 'ComfyUI-Workbench'
    if not (packed / 'ComfyUI-Workbench.exe').is_file():
        raise RuntimeError('Build the application first')
    output.mkdir(parents=True, exist_ok=True)
    program = output / 'ComfyUI-Workbench'
    if program.exists():
        if not args.resume:
            raise RuntimeError(f'Refusing to overwrite existing release: {program}')
        allowed = {'ComfyUI-Workbench.exe', '_internal', 'README.md', 'LICENSE',
                   'THIRD_PARTY_NOTICES.md', 'ROADMAP.md', 'licenses'}
        if any(p.name not in allowed for p in program.iterdir()):
            raise RuntimeError('Existing release contains unrecognized files; cannot resume')
        # Resume only the exact runtime copied by the previous invocation.
        for original in packed.rglob('*'):
            if not original.is_file() or original.relative_to(packed).parts[0] not in {'ComfyUI-Workbench.exe', '_internal'}:
                continue
            copied = program / original.relative_to(packed)
            if not copied.is_file() or copied.stat().st_size != original.stat().st_size:
                raise RuntimeError(f'Incomplete runtime copy: {copied}')
    # Explicit runtime allowlist: no data/, backups, test folders or user models.
    else:
        program.mkdir()
        shutil.copy2(packed / 'ComfyUI-Workbench.exe', program)
        shutil.copytree(packed / '_internal', program / '_internal')
    for name in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'ROADMAP.md'):
        shutil.copy2(ROOT / name, program)
    collect_licenses(program / 'licenses')

    source_zip = output / f'ComfyUI-Workbench-{__version__}-source.zip'
    zip_files(source_zip, [(p, 'comfy-workbench/' + p.relative_to(ROOT).as_posix()) for p in source_files()])
    if (program / '_internal' / 'onnxruntime' / 'capi' / 'onnxruntime_providers_cuda.dll').is_file():
        flavor = 'gpu'
    elif (program / '_internal' / 'onnxruntime').is_dir():
        flavor = 'cpu'
    else:
        flavor = 'basic'
    runtime_zip = output / f'ComfyUI-Workbench-{__version__}-windows-{flavor}.zip'
    zip_files(runtime_zip, [(p, 'ComfyUI-Workbench/' + p.relative_to(program).as_posix())
                            for p in sorted(program.rglob('*')) if p.is_file()])
    checksums = []
    for path in (source_zip, runtime_zip):
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        checksums.append(f'{digest}  {path.name}')
    (output / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='utf-8')
    print(f'RELEASE_OK {output}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
