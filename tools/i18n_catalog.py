"""Check and maintain the interface translation catalogs.

The frontend uses Simplified Chinese source strings as message keys:
``t('搜索')`` or ``t('共 {count} 条', { count })``; ``tc('按钮', '下载')`` gives a
text a separate entry keyed ``按钮|下载``. Also included are the texts the server
sends for display listed in ``lib/serverTexts.ts``. Each other language has a
catalog in ``frontend/src/locales/<code>.ts`` that maps every key to its
translation. ``check`` fails when a catalog misses a key, keeps a key the code
no longer uses, or changes the ``{placeholder}`` names of a message;
``missing`` lists the keys a catalog still lacks; ``sync`` rewrites the
catalogs in source order and drops unused keys.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / 'frontend' / 'src'
LOCALE_ROOT = SOURCE_ROOT / 'locales'
SERVER_TEXTS = SOURCE_ROOT / 'lib' / 'serverTexts.ts'
LOCALES = ('zh-TW', 'en', 'ja', 'fr', 'es', 'ru', 'ar')
MESSAGE_CALL = re.compile(r"(?<![\w.$])t\(\s*'((?:[^'\\\n]|\\.)*)'")
SERVER_TEXT = re.compile(r"^  '((?:[^'\\\n]|\\.)*)',$", re.M)
CONTEXT_CALL = re.compile(r"(?<![\w.$])tc\(\s*'((?:[^'\\\n]|\\.)*)',\s*'((?:[^'\\\n]|\\.)*)'")
DYNAMIC_CALL = re.compile(r"(?<!function )(?<![\w.$])t\(\s*(?!'|\))")
DYNAMIC_CONTEXT_CALL = re.compile(r"(?<!function )(?<![\w.$])tc\(\s*(?!'[^'\n]*',\s*')")
PLACEHOLDER = re.compile(r'\{(\w+)\}')
ENTRY = re.compile(r'^  ("(?:[^"\\]|\\.)*"): ("(?:[^"\\]|\\.)*"),$')
HEADER = (
    '// Interface translations keyed by the Simplified Chinese source text.\n'
    '// Keep {placeholders} unchanged; check with `python tools/i18n_catalog.py check`.\n'
    'const messages: Record<string, string> = {\n'
)
FOOTER = '}\n\nexport default messages\n'
_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', "'": "'", '"': '"'}


class CatalogError(ValueError):
    pass


def _unescape(value: str) -> str:
    return re.sub(r'\\(.)', lambda match: _ESCAPES.get(match.group(1), match.group(1)), value)


def source_files(root: Path = SOURCE_ROOT) -> list[Path]:
    return sorted(
        path
        for path in root.rglob('*')
        if path.suffix in {'.ts', '.tsx'}
        and LOCALE_ROOT not in path.parents
        and not path.name.endswith('.d.ts')
        and '.test.' not in path.name
    )


def extract_messages(root: Path = SOURCE_ROOT) -> list[str]:
    """Message keys in order of first use, checking that every t() call is literal."""

    keys: dict[str, None] = {}
    problems: list[str] = []
    for path in source_files(root):
        text = path.read_text(encoding='utf-8')
        if 'i18n' not in text:
            continue
        for pattern in (DYNAMIC_CALL, DYNAMIC_CONTEXT_CALL):
            for match in pattern.finditer(text):
                line = text.count('\n', 0, match.start()) + 1
                problems.append(f'{path.relative_to(ROOT).as_posix()}:{line}: t() and tc() need string literals')
        for match in MESSAGE_CALL.finditer(text):
            keys.setdefault(_unescape(match.group(1)), None)
        # tc('context', 'text') is keyed as "context|text".
        for match in CONTEXT_CALL.finditer(text):
            keys.setdefault(f'{_unescape(match.group(1))}|{_unescape(match.group(2))}', None)
    if problems:
        raise CatalogError('\n'.join(problems))
    # Chinese texts the server sends for display (see lib/serverTexts.ts).
    for match in SERVER_TEXT.finditer(SERVER_TEXTS.read_text(encoding='utf-8')):
        keys.setdefault(_unescape(match.group(1)), None)
    return list(keys)


def read_catalog(code: str, root: Path = LOCALE_ROOT) -> dict[str, str]:
    path = root / f'{code}.ts'
    if not path.is_file():
        return {}
    catalog: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        match = ENTRY.match(line)
        if match:
            key = json.loads(match.group(1))
            if key in catalog:
                raise CatalogError(f'{path.name}:{number}: duplicate entry {key!r}')
            catalog[key] = json.loads(match.group(2))
        elif line.startswith('  "'):
            raise CatalogError(f'{path.name}:{number}: entry is not a single-line "key": "value" pair')
    return catalog


def write_catalog(code: str, catalog: dict[str, str], keys: list[str], root: Path = LOCALE_ROOT) -> None:
    lines = [
        f'  {json.dumps(key, ensure_ascii=False)}: {json.dumps(catalog[key], ensure_ascii=False)},\n'
        for key in keys
        if catalog.get(key)
    ]
    (root / f'{code}.ts').write_text(HEADER + ''.join(lines) + FOOTER, encoding='utf-8', newline='\n')


def catalog_problems(code: str, keys: list[str], catalog: dict[str, str]) -> list[str]:
    problems: list[str] = []
    used = set(keys)
    for key in keys:
        value = catalog.get(key)
        if not value:
            problems.append(f'{code}: missing {key!r}')
        elif set(PLACEHOLDER.findall(key)) != set(PLACEHOLDER.findall(value)):
            problems.append(f'{code}: placeholders differ for {key!r}: {value!r}')
    problems.extend(f'{code}: unused {key!r}' for key in catalog if key not in used)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('check', 'missing', 'sync'))
    parser.add_argument('--locale', choices=LOCALES, action='append')
    args = parser.parse_args(argv)
    try:
        keys = extract_messages()
        codes = args.locale or list(LOCALES)
        if args.command == 'sync':
            for code in codes:
                write_catalog(code, read_catalog(code), keys)
            print(f'{len(keys)} messages, {len(codes)} catalogs rewritten')
            return 0
        if args.command == 'missing':
            for code in codes:
                catalog = read_catalog(code)
                for key in keys:
                    if not catalog.get(key):
                        print(json.dumps({'locale': code, 'key': key}, ensure_ascii=False))
            return 0
        problems = [problem for code in codes for problem in catalog_problems(code, keys, read_catalog(code))]
    except CatalogError as exc:
        print(exc, file=sys.stderr)
        return 1
    if problems:
        print('\n'.join(problems[:200]), file=sys.stderr)
        if len(problems) > 200:
            print(f'... {len(problems) - 200} more', file=sys.stderr)
        return 1
    print(json.dumps({'ok': True, 'messages': len(keys), 'locales': codes}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
