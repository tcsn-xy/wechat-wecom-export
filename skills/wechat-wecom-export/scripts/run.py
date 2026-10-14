#!/usr/bin/env python3
"""Run a bundled project command using the selected deployment environment."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('script', help='Basename of a script in the deployment scripts directory')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args=parser.parse_args();root=args.project.expanduser().resolve()
    if Path(args.script).name != args.script or not args.script.endswith('.py'):
        parser.error('Specify only a Python script basename')
    script=root/'scripts'/args.script
    if script.is_symlink() or not script.is_file():parser.error('Script unavailable')
    interpreter=root/'.venv/bin/python'
    if not interpreter.exists():parser.error('Run scripts/deploy.py bootstrap with Python 3.11 first')
    return subprocess.run([str(interpreter),str(script),*args.arguments],cwd=root).returncode


if __name__ == '__main__':sys.exit(main())
