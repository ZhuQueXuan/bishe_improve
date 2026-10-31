"""Consistent SQLite backup or restore-to-new-copy; never overwrites a database."""
import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def snapshot(source, destination):
    source = source.resolve(strict=True)
    destination = destination.resolve()
    formal = Path(__file__).resolve().parents[1] / 'knowledge_graph_backend/db.sqlite3'
    if destination == source or destination == formal.resolve():
        raise ValueError('Output must be a new copy, never the source or the running account DB.')
    if not destination.parent.is_dir():
        raise ValueError('Output directory must already exist.')
    # Exclusive creation also rejects existing files, hard links and symlinks.
    # A failed partial copy is retained for diagnosis; no source is modified.
    with destination.open('xb'):
        pass
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=15)) as original:
        with closing(sqlite3.connect(destination, timeout=15)) as backup:
            original.backup(backup, pages=128, sleep=0.05)
            if backup.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Backup integrity check failed. Keep original database.')
            if backup.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Backup foreign key check failed. Keep original database.')
    return {
        'passed': True,
        'source': str(source),
        'backup': str(destination),
        'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'Consistent SQLite copy; source opened read-only. No formal restore or overwrite.',
    }


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=root / 'bishe/knowledge_graph_backend/db.sqlite3')
    parser.add_argument('--output', type=Path, help='New file only. Parent directory must exist.')
    args = parser.parse_args()
    try:
        if args.output is None:
            folder = root / 'temp/account_backups'
            folder.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8] + '.sqlite3'
            args.output = folder / name
        result = snapshot(args.source, args.output)
        with args.output.with_suffix('.report.json').open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, sqlite3.Error, ValueError) as error:
        print('Backup failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
