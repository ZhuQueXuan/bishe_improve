"""Explicit local 1.1.0 profile; default/test settings keep release gates off."""
from .settings import *  # noqa: F403,F401
import json,os

record=json.loads(Path(os.getenv('NATURAL_MEMORY_RELEASE_MANIFEST',str(BASE_DIR.parent/'data/natural_memory_v1/runtime_release_v1_1_0.json'))).read_text(encoding='utf-8'))
if not record.get('ready') or record.get('version')!='1.1.0':raise RuntimeError('release_not_ready')
if os.getenv('NATURAL_MEMORY_DATABASE'):
    DATABASES['default']['NAME']=os.environ['NATURAL_MEMORY_DATABASE']
NATURAL_MEMORY_FORMAL_ENABLED=True
NATURAL_MEMORY_RELEASE_READY=True
NATURAL_MEMORY_FORMAL_DATABASE=str(DATABASES['default']['NAME'])
NATURAL_MEMORY_BACKGROUND_ENABLED=True
NATURAL_MEMORY_DISPATCH_CANDIDATE_ENABLED=True
NATURAL_MEMORY_LIVE_DISPATCH_ENABLED=True
NATURAL_MEMORY_PREFERENCE_AUTO_RELEASED=True
NATURAL_MEMORY_RELEASE_VERSION='1.1.0'
