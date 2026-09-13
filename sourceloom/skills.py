"""Deploy the complete skill package and inject its unabridged entry closure."""

import json
from pathlib import Path
import re
import shutil

from .store import digest

SKIP = {'.git', '__pycache__', '.pytest_cache', '.local', 'node_modules', '.venv'}


def instruction_files(root):
    """Follow every local Markdown reference, including conditional references."""
    root = Path(root).resolve()
    pending, found = ['SKILL.md'], {}
    while pending:
        name = pending.pop(0)
        if name in found:
            continue
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('写作技能引用缺失或越界：' + name)
        text = path.read_text(encoding='utf-8-sig')
        found[name] = text
        references = re.findall(r'\]\(([^)]+)\)', text)
        references += re.findall(r'`([^`\n]+\.md)`', text)
        for target in references:
            target = target.strip('<>').split('#')[0]
            if '://' in target or not target.lower().endswith('.md'):
                continue
            child = (path.parent / target).resolve()
            if not child.is_file() and (root / target).is_file():
                child = (root / target).resolve()
            if not child.is_relative_to(root):
                raise ValueError('技能引用越出完整包')
            pending.append(child.relative_to(root).as_posix())
    if not {'references/format-rules.md', 'references/explanation-framework.md',
            'references/formula-explanation.md'} <= found.keys():
        raise ValueError('技能入口没有引用完整核心规则')
    return found


def deploy_skill(source, data_root):
    """Copy every package file, not just the files injected in the prompt."""
    source = Path(source).resolve()
    if not (source / 'SKILL.md').is_file():
        raise ValueError('真实生成需要指定完整写作技能包')
    # A deployed bundle already has a generated manifest. Validate that bundle
    # before copying it, but never include the manifest in its own digest.
    if (source / 'package-manifest.json').is_file():
        load_bundle(source)
    files = {}
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if relative.as_posix() == 'package-manifest.json':
            continue
        if any(part in SKIP for part in relative.parts) or path.is_dir():
            continue
        if path.is_symlink():
            raise ValueError('技能包不能含有链接到外部的文件')
        files[relative.as_posix()] = {'sha256': digest(path.read_bytes()), 'bytes': path.stat().st_size}
    version = digest(files)
    target = Path(data_root).resolve() / 'skills' / version
    if not (target / 'package-manifest.json').is_file():
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copyfile(source / name, dest)
            if digest(dest.read_bytes()) != files[name]['sha256']:
                raise ValueError('技能部署内容与源文件不一致')
        (target / 'package-manifest.json').write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding='utf-8')
    return load_bundle(target, version)


def load_bundle(root, expected=None):
    root = Path(root).resolve()
    files = json.loads((root / 'package-manifest.json').read_text(encoding='utf-8'))
    version = digest(files)
    if expected is not None and version != expected:
        raise ValueError('冻结的技能清单已变化')
    for name, entry in files.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or digest(path.read_bytes()) != entry['sha256']:
            raise ValueError('冻结的完整技能包校验失败')
    instructions = instruction_files(root)
    return {'package_digest': version, 'instruction_digest': digest(instructions),
            'root': str(root), 'files': files, 'instructions': instructions}


def full_prompt(bundle):
    return '\n\n'.join('===== SKILL FILE: ' + name + ' =====\n' + text
                       for name, text in bundle['instructions'].items())


def rule_catalog(instructions):
    rules = {}
    for name, text in instructions.items():
        for match in re.finditer(r'^- `(FMT-\d+|EXPL-\d+)` (.+)$', text, re.M):
            rules[match[1]] = {'file': name, 'text': match[2]}
        if name == 'references/formula-explanation.md':
            number = 0
            for line in text.splitlines():
                if line.startswith('- '):
                    number += 1
                    rules[f'FORMULA-{number:03d}'] = {'file': name, 'text': line[2:]}
    return rules
