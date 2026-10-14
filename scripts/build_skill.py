#!/usr/bin/env python3
"""Build a complete standalone Skill from the explicit public source manifest."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from privacy_scan import manifest_files, scan_directory, scan_zip

ROOT=Path(__file__).resolve().parents[1]
NAME='wechat-wecom-export'


def checksums(folder):
    return {str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob('*')) if p.is_file()}


def build(output, install_to=None):
    findings=scan_directory(ROOT, [])
    if findings:raise ValueError('Privacy scan failed; resolve findings before packaging')
    output=Path(output).expanduser().resolve();output.mkdir(parents=True,exist_ok=True)
    template=ROOT/'skills'/NAME
    with tempfile.TemporaryDirectory(prefix='skill-build-') as temp:
        skill=Path(temp)/NAME;skill.mkdir()
        prefix='skills/'+NAME+'/'
        for name in manifest_files(ROOT):
            if name.startswith(prefix):
                target=skill/name[len(prefix):];target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(ROOT/name,target)
        project=skill/'assets/project';project.mkdir(parents=True)
        for name in manifest_files(ROOT):
            target=project/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(ROOT/name,target)
        (skill/'assets/project-checksums.json').write_text(json.dumps(checksums(project),indent=2)+'\n')
        archive=output/(NAME+'-skill.zip')
        temporary=archive.with_suffix('.tmp')
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(skill.rglob('*')):
                if not path.is_file():continue
                item=zipfile.ZipInfo(str(path.relative_to(skill.parent)),date_time=(2026,1,1,0,0,0))
                item.external_attr=(path.stat().st_mode&0o777)<<16
                item.compress_type=zipfile.ZIP_DEFLATED;zipped.writestr(item,path.read_bytes())
        if scan_zip(temporary,[]):temporary.unlink();raise ValueError('Packaged privacy scan failed')
        temporary.replace(archive)
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        (output/'SHA256SUMS').write_text(digest+'  '+archive.name+'\n')
        if install_to:
            destination=Path(install_to).expanduser().resolve()/NAME
            if destination.exists():
                if checksums(destination)!=checksums(skill):raise ValueError('Installed skill differs; review update explicitly')
            else:
                destination.parent.mkdir(parents=True,exist_ok=True)
                staging=Path(tempfile.mkdtemp(prefix='.install-skill-',dir=destination.parent))
                try:
                    shutil.copytree(skill,staging/'skill');(staging/'skill').rename(destination)
                finally:shutil.rmtree(staging)
        return dict(archive=str(archive),sha256=digest,source_files=len(manifest_files(ROOT)))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=ROOT/'dist')
    parser.add_argument('--install',action='store_true',help='Install to local Codex skills without running deployment')
    parser.add_argument('--skills-dir',type=Path,default=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'skills')
    args=parser.parse_args()
    print(json.dumps(build(args.out,args.skills_dir if args.install else None),indent=2))


if __name__=='__main__':main()
