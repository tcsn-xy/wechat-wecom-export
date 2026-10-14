#!/usr/bin/env python3
"""Install the bundled project without replacing data or starting services."""
import argparse
import hashlib
import json
from pathlib import Path
import os
import shutil
import tempfile

SKILL = Path(__file__).resolve().parents[1]


def install(target):
    source = SKILL / 'assets/project'
    checksums = json.loads((SKILL / 'assets/project-checksums.json').read_text())
    target = Path(target).expanduser().absolute()
    if target.is_symlink():raise ValueError('Deployment directory must not be a symlink')
    for name, checksum in checksums.items():
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts:raise ValueError('Invalid packaged path')
        origin = source / rel
        if origin.is_symlink() or not origin.resolve().is_relative_to(source.resolve()):raise ValueError('Unsafe packaged path')
        if hashlib.sha256(origin.read_bytes()).hexdigest() != checksum:raise ValueError('Packaged source checksum mismatch')
        destination = target / rel
        if target.exists() and destination.exists():
            if destination.is_symlink() or not destination.resolve().is_relative_to(target.resolve()):raise ValueError('Unsafe existing deployment path')
            if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != checksum:
                raise ValueError('Existing source differs; choose a fresh directory or review an explicit update')
    if target.exists():
        # Refuse symlink parent directories before filling missing files.
        for name in checksums:
            destination = target / name
            if not destination.resolve().is_relative_to(target.resolve()):raise ValueError('Unsafe existing deployment path')
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, destination)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.wechat-deploy-', dir=target.parent))
        try:
            for name in checksums:
                destination = staging / name;destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, destination)
            staging.rename(target)
        finally:
            if staging.exists():shutil.rmtree(staging)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, default=Path.home() / 'wechat-wecom-export')
    args = parser.parse_args()
    print('Installed deployment: ' + str(install(args.target)))
    print('Next: python3.11 scripts/deploy.py bootstrap from that directory')


if __name__ == '__main__':main()
