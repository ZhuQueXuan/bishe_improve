"""Interactive local credential setup; does not make HTTP requests."""
import getpass
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
from api.embedding import CONFIG_PATH, EmbeddingConfig, EmbeddingError


def main():
    print('Paste the Beijing API Host (hostname only; shown beside your API Key).')
    host = input('API Host: ').strip()
    secret = getpass.getpass('API Key (hidden): ').strip()
    try:
        EmbeddingConfig(host, secret)
    except EmbeddingError as error:
        print('Configuration rejected: ' + str(error))
        return 1
    if CONFIG_PATH.exists():
        backup = PROJECT.parent / 'temp' / 'embedding' / ('config-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir(parents=True)
        shutil.copy2(CONFIG_PATH, backup / '.env.embedding')
    CONFIG_PATH.write_text('DASHSCOPE_API_HOST=' + host + '\nDASHSCOPE_API_KEY=' + secret + '\n', encoding='utf-8')
    print('Saved locally. The key was not displayed. No API request was made.')
    return 0


if __name__ == '__main__': raise SystemExit(main())
