"""Local document catalog: preview/import/activate/disable/list/check/search. No model calls."""
import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
from api.document_library import DEFAULT_ROOT, DocumentLibrary, LibraryError, inspect_pdf, lexical_search


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest='command',required=True)
    for name in ('preview','import'):
        p = commands.add_parser(name)
        p.add_argument('--pdf',type=Path,required=True)
        p.add_argument('--metadata',type=Path,required=True)
    commands.add_parser('list')
    commands.add_parser('check')
    p = commands.add_parser('activate')
    p.add_argument('version_id')
    p = commands.add_parser('disable')
    p.add_argument('doc_id')
    p = commands.add_parser('search')
    p.add_argument('query')
    p.add_argument('--limit',type=int,default=5)
    args = parser.parse_args()
    library = DocumentLibrary(args.root)
    try:
        if args.command in ('preview','import'):
            metadata = json.loads(args.metadata.read_text(encoding='utf-8-sig'))
            if args.command == 'import':
                result = library.import_pdf(args.pdf,metadata)
            else:
                prepared = inspect_pdf(args.pdf,metadata)
                result = {'version_id':prepared['version_id'], 'total_pages':len(prepared['pages']),
                          'selected_pages':metadata['selected_pages'], 'chunks':len(prepared['chunks']),
                          'characters':sum(len(c['text']) for c in prepared['chunks']),
                          'catalog_written':False}
        elif args.command == 'list':
            result = library.versions()
        elif args.command == 'activate':
            library.activate(args.version_id)
            result = {'active_for_local_retrieval':args.version_id,'vector_readiness_checked':False}
        elif args.command == 'disable':
            library.disable(args.doc_id)
            result = {'disabled':args.doc_id,'old_data_retained':True}
        elif args.command == 'check':
            result = {'versions':[{ 'id':v['id'],'source_verified':library.verify(v['id'])}
                                  for v in library.versions()]}
        else:
            result = {'mode':'lexical_only_no_embedding','results':lexical_search(library,args.query,args.limit)}
        print(json.dumps({'ok':True,'library_root':str(library.root),'external_model_calls':0,'result':result},
                         ensure_ascii=False,indent=2))
        return 0
    except (LibraryError,OSError,ValueError) as error:
        # Metadata is public; never load or print credential files in this tool.
        print(json.dumps({'ok':False,'error':str(error),'external_model_calls':0},ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
