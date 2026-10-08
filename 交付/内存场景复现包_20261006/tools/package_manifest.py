from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'PACKAGE_MANIFEST.json'

def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def inventory() -> list[dict]:
    rows = []
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file() or path.resolve() == MANIFEST.resolve():
            continue
        if '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        rows.append({'path': path.relative_to(ROOT).as_posix(),
                     'bytes': path.stat().st_size, 'sha256': digest(path)})
    return rows

def write() -> dict:
    rows = inventory()
    data = {'schema': 'memscenes-delivery-manifest-v1',
            'created_utc': datetime.now(timezone.utc).isoformat(),
            'file_count': len(rows), 'total_bytes': sum(row['bytes'] for row in rows),
            'files': rows}
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return data

def verify() -> tuple[bool, dict]:
    supplied = json.loads(MANIFEST.read_text(encoding='utf-8'))
    expected = {row['path']: row for row in supplied.get('files', [])}
    actual = {row['path']: row for row in inventory()}
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(path for path in set(expected) & set(actual)
                     if expected[path]['sha256'] != actual[path]['sha256'] or
                     expected[path]['bytes'] != actual[path]['bytes'])
    report = {'verified': not missing and not extra and not changed,
              'expected_files': len(expected), 'actual_files': len(actual),
              'missing': missing, 'extra': extra, 'changed': changed}
    return report['verified'], report

def main() -> int:
    parser = argparse.ArgumentParser(description='Create or verify all package-file SHA256 hashes.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--write', action='store_true')
    group.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.write:
        data = write()
        print(json.dumps({'manifest': str(MANIFEST.relative_to(ROOT)),
                          'file_count': data['file_count'], 'total_bytes': data['total_bytes']}, indent=2))
        return 0
    ok, report = verify()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 2

if __name__ == '__main__':
    raise SystemExit(main())
