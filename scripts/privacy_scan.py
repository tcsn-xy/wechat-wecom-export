#!/usr/bin/env python3
"""Scan a release manifest, zip or Git history; report locations, never values."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

PATTERNS = {
    'github_token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})'),
    'api_token': re.compile(rb'sk-[A-Za-z0-9_-]{20,}'),
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'personal_home': re.compile(rb'/Users' + rb'/(?!example(?:/|\b))[^\s/\"\']+'),
    'credential_url': re.compile(rb'https?://[^\s/]+:[^\s/]+@'),
    'personal_chat_id': re.compile(rb'(?<![0-9])(?!(?:1000000000[12])@)[0-9]{8,}@chatroom'),
    'enterprise_user_id': re.compile(rb'(?<![0-9])1688[0-9]{10,}(?![0-9])'),
}
PRIVATE_COMPONENTS = {'.runtime', '.venv', 'keys', 'data', 'export', 'media_store', 'reference', '.scratch', '__pycache__'}
PRIVATE_SUFFIXES = {'.db', '.db-wal', '.db-shm', '.dat', '.dmp', '.jsonl', '.log', '.pyc', '.mp4', '.wav', '.silk'}


def allowed_path(name):
    path = Path(name)
    return (not path.is_absolute() and '..' not in path.parts and not PRIVATE_COMPONENTS.intersection(path.parts)
            and path.suffix not in PRIVATE_SUFFIXES and not any(p.startswith('.env') for p in path.parts))


def scan_blob(name, data, deny):
    findings = []
    for category, pattern in PATTERNS.items():
        for match in pattern.finditer(data):
            findings.append(dict(location=name, line=data[:match.start()].count(b'\n') + 1, category=category))
    for value in deny:
        if value and value in data:
            findings.append(dict(location=name, category='private_denylist'))
    return findings


def manifest_files(root):
    manifest = json.loads((root / 'release-manifest.json').read_text())
    if not isinstance(manifest, list) or len(manifest) != len(set(manifest)):
        raise ValueError('Invalid or duplicate release manifest')
    for name in manifest:
        if not isinstance(name, str) or not allowed_path(name):
            raise ValueError('Private or unsafe path in release manifest')
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError('Missing or unsafe release file')
    return manifest


def scan_directory(root, deny):
    root = Path(root).resolve()
    names = manifest_files(root)
    findings = []
    for name in names:
        findings += scan_blob(name, (root / name).read_bytes(), deny)
    # A tracked private or unexpected file must not slip past the manifest.
    if (root / '.git').exists():
        result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True, check=True)
        for name in result.stdout.decode().split('\0'):
            if name and (name not in names or not allowed_path(name)):
                findings.append(dict(location=name, category='unexpected_tracked_path'))
    return findings


def scan_git(root, deny):
    findings = []
    objects = subprocess.check_output(['git', 'rev-list', '--objects', '--all'], cwd=root).decode().splitlines()
    for line in objects:
        oid, _, name = line.partition(' ')
        typ = subprocess.check_output(['git', 'cat-file', '-t', oid], cwd=root).strip()
        if name and not allowed_path(name):
            findings.append(dict(location='git:' + oid[:12], category='private_history_path'))
        if typ in (b'blob', b'commit', b'tag'):
            blob = subprocess.check_output(['git', 'cat-file', '-p', oid], cwd=root)
            findings += scan_blob('git:' + oid[:12], blob, deny)
    return findings


def scan_zip(path, deny):
    findings = []
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            if item.is_dir():continue
            if not allowed_path(item.filename):
                findings.append(dict(location=item.filename, category='private_archive_path'))
            findings += scan_blob(item.filename, archive.read(item), deny)
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--git-history', action='store_true')
    parser.add_argument('--zip', type=Path)
    parser.add_argument('--deny-file', type=Path, help='Private JSON list of additional sensitive values')
    args = parser.parse_args()
    deny = [] if not args.deny_file else [value.encode() for value in json.loads(args.deny_file.read_text())]
    try:
        findings = scan_zip(args.zip, deny) if args.zip else scan_directory(args.directory, deny)
        if args.git_history:findings += scan_git(args.directory, deny)
        print(json.dumps(dict(ok=not findings, findings=findings), ensure_ascii=False, indent=2))
        return bool(findings)
    except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile):
        print(json.dumps(dict(ok=False, error_category='invalid_release_input')))
        return 1


if __name__ == '__main__':sys.exit(main())
