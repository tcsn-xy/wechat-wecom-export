"""Bind profile and database namespaces inside one captured snapshot."""
import json
from pathlib import Path
import re


def profile_accounts(raw_root):
    from .extract import datasets
    root=Path(raw_root)
    profiles=[p.name for p in root.glob('Profiles/*') if p.is_dir()]
    accounts=set()
    for ds in datasets(root):
        if 'Backup' in ds.name:continue
        match=re.match(r'(\d+)',ds.name)
        accounts.add(match.group(1) if match else ds.name)
    result={}
    explicit_accounts=set()
    explicit=root/'_profile_accounts.json'
    if explicit.exists():
        mapping=json.loads(explicit.read_text(encoding='utf-8'))
        explicit_accounts=set(mapping)
        for account,profile in mapping.items():
            if profile not in profiles or profile in result:
                raise ValueError('账号与媒体档案映射无效或重复')
            result[profile]=account
    for profile in profiles:
        matches=[a for a in accounts if profile==a or profile.startswith(a+'_')]
        if len(matches)==1 and matches[0] not in explicit_accounts:
            result.setdefault(profile,matches[0])
    # A singleton match is allowed only when the database is present in THIS raw snapshot.
    if len(accounts)==len(profiles)==1:
        result.setdefault(profiles[0],next(iter(accounts)))
    return result


def profile_for(raw_root,account):
    root=Path(raw_root)
    mapping=profile_accounts(root)
    profiles=[p for p in root.glob('Profiles/*') if (p/'Caches').is_dir()]
    matches=[p for p in profiles if mapping.get(p.name)==account or (p.name==account and p.name not in mapping)]
    if len(matches)==1:return matches[0]
    if profiles:raise ValueError('无法唯一对应账号 %s 的媒体目录；可在原始快照提供 _profile_accounts.json' % account)
    return None
