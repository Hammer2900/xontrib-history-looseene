import sys
import time
import builtins
from xonsh.events import events
from .backend import SearchEngineHistory
from .ui import start_search_ui


def _load_xontrib_(xsh, **kwargs):
    if xsh.env.get('XONTRIB_LOOSEENE_LOADED'):
        return
    current_hist = getattr(xsh, 'history', None)
    if current_hist is None and hasattr(builtins, '__xonsh__'):
        current_hist = getattr(builtins.__xonsh__, 'history', None)
    if current_hist and hasattr(current_hist, 'run_compaction') and hasattr(current_hist, 'engine'):
        return
    xsh.env['XONTRIB_LOOSEENE_LOADED'] = True
    xsh.env['XONSH_HISTORY_BACKEND'] = SearchEngineHistory
    global_history = SearchEngineHistory()
    xsh.history = global_history
    print('Looseene: History backend loaded (with Counts & Comments).', file=sys.stderr)

    @events.on_ptk_create
    def custom_keybindings(bindings, **kw):
        @bindings.add('c-r')
        async def _(event):
            current_line = event.current_buffer.text
            await start_search_ui(event, initial_text=current_line)

    def _hsearch(args):
        if not args:
            print('Usage: hsearch <query>')
            return
        query = ' '.join(args)
        hist = xsh.history
        if hasattr(hist, 'search'):
            print(f'Searching for: {query}...')
            results = hist.search(query, limit=5)
            if not results:
                print('No matches found.')
            for i, doc in enumerate(results):
                cmd = doc.get('inp', '').strip().replace('\n', ' ')
                cnt = doc.get('cnt', 1)
                cmt = doc.get('cmt', '')
                meta = f'({cnt})'
                if cmt:
                    meta += f' # {cmt}'
                print(f'{i + 1}. {meta} {cmd}')
        else:
            print('Error: Looseene backend not active.')

    xsh.aliases['hsearch'] = _hsearch
    xsh.aliases['hs'] = _hsearch

    def _compact(args):
        if hasattr(xsh.history, 'run_compaction'):
            xsh.history.run_compaction()
        else:
            print('Looseene backend not active.')

    xsh.aliases['history-compact'] = _compact

    def _add_comment(args):
        if len(args) < 2:
            print('Usage: hs-comment <partial_command> <comment_text>')
            return
        comment_text = args[-1]
        query_parts = args[:-1]
        query = ' '.join(query_parts)
        if hasattr(xsh.history, 'search') and hasattr(xsh.history, 'engine'):
            results = xsh.history.search(query, limit=1)
            if results:
                original = results[0]
                cmd_str = original.get('inp', '')
                print(f"Adding comment to: '{cmd_str}'")
                new_doc = original.copy()
                new_doc['id'] = time.time_ns()
                new_doc['cmt'] = comment_text
                xsh.history.engine.add(new_doc)
                xsh.history.engine.flush()
                print('Comment added successfully.')
            else:
                print(f"Command matching '{query}' not found.")
        else:
            print('Looseene backend not active.')

    xsh.aliases['hs-comment'] = _add_comment
    try:
        if hasattr(xsh.history, 'engine') and len(xsh.history.engine.segments) > 50:
            print("Looseene: Many history segments detected (>50). Run 'history-compact'.", file=sys.stderr)
    except:
        pass
