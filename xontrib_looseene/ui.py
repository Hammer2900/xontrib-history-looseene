import builtins
import re
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, Window, VSplit
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.widgets import Frame
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.shortcuts import input_dialog


def get_history_backend():
    if hasattr(builtins, '__xonsh__'):
        hist = builtins.__xonsh__.history
        if hasattr(hist, 'search'):
            return hist
    return None


async def start_search_ui(event, initial_text=''):
    history = get_history_backend()
    if not history:
        print('Looseene backend is not active!')
        return
    state = {'docs': [], 'selected_index': 0}
    try:
        if initial_text:
            state['docs'] = history.search(initial_text, limit=20)
        else:
            gen = history.items(newest_first=True)
            for _ in range(20):
                state['docs'].append(next(gen))
    except StopIteration:
        pass
    search_buffer = Buffer(multiline=False)
    if initial_text:
        search_buffer.text = initial_text
        search_buffer.cursor_position = len(initial_text)

    def get_content():
        query = search_buffer.text.strip()
        if query:
            state['docs'] = history.search(query, limit=20)
        elif not query and (not state['docs'] or len(state['docs']) < 5):
            state['docs'] = []
            try:
                gen = history.items(newest_first=True)
                for _ in range(20):
                    state['docs'].append(next(gen))
            except:
                pass
        if not state['docs']:
            return [('ansibrightblack', '  No results found...')]
        if state['selected_index'] >= len(state['docs']):
            state['selected_index'] = len(state['docs']) - 1
        if state['selected_index'] < 0:
            state['selected_index'] = 0
        highlight_regex = None
        if query:
            tokens = [re.escape(t) for t in query.split() if t]
            if tokens:
                pattern = f'({"|".join(tokens)})'
                try:
                    highlight_regex = re.compile(pattern, re.IGNORECASE)
                except re.error:
                    pass
        fragments = []
        for i, doc in enumerate(state['docs']):
            cmd = doc.get('inp', '').strip()
            count = doc.get('cnt', 1)
            comment = doc.get('cmt', '')
            cmd_display = cmd.replace('\n', ' ')
            if i == state['selected_index']:
                prefix_style = 'reverse ansigreen'
                prefix = '> '
            else:
                prefix_style = ''
                prefix = '  '
            fragments.append((prefix_style, prefix))
            fragments.append(('ansiyellow', f'({count}) '))
            if highlight_regex:
                parts = highlight_regex.split(cmd_display)
                for part in parts:
                    if not part:
                        continue
                    if highlight_regex.fullmatch(part):
                        if i == state['selected_index']:
                            fragments.append(('reverse ansigreen ansibrightyellow bold', part))
                        else:
                            fragments.append(('ansibrightyellow bold', part))
                    else:
                        fragments.append((prefix_style, part))
            else:
                fragments.append((prefix_style, cmd_display))
            if comment:
                if len(comment) > 30:
                    comment = comment[:27] + '...'
                fragments.append(('ansiblue italic', f'  # {comment}'))
            fragments.append(('', '\n'))
        return fragments

    result_control = FormattedTextControl(text=get_content)

    def on_text_changed(_):
        state['selected_index'] = 0

    search_buffer.on_text_changed += on_text_changed
    kb = KeyBindings()

    @kb.add('c-c')
    @kb.add('c-g')
    @kb.add('c-d')
    def _exit(e):
        e.app.exit(result=search_buffer.text)

    @kb.add('up')
    def _up(e):
        if state['selected_index'] > 0:
            state['selected_index'] -= 1

    @kb.add('down')
    def _down(e):
        if state['selected_index'] < len(state['docs']) - 1:
            state['selected_index'] += 1

    @kb.add('enter')
    def _submit(e):
        if state['docs'] and 0 <= state['selected_index'] < len(state['docs']):
            cmd = state['docs'][state['selected_index']].get('inp', '')
            e.app.exit(result=cmd)
        else:
            e.app.exit(result=search_buffer.text)

    @kb.add('f3')
    async def _add_comment_ui(e):
        if not state['docs']:
            return
        current_doc = state['docs'][state['selected_index']]
        old_comment = current_doc.get('cmt', '')
        new_comment = await input_dialog(
            title='Add/Edit Comment', text=f'Command: {current_doc.get("inp", "")}\n', default=old_comment
        ).run_async()
        if new_comment is not None:
            if hasattr(history, 'update_comment'):
                history.update_comment(current_doc, new_comment)
                current_doc['cmt'] = new_comment
                query = search_buffer.text
                if query:
                    state['docs'] = history.search(query, limit=20)

    results_window = Window(content=result_control, height=Dimension(min=10, max=10), wrap_lines=False)
    search_window = Window(BufferControl(buffer=search_buffer), height=1)
    label_window = Window(
        content=FormattedTextControl(text=[('ansiblue bold', 'Search: ')]), height=1, dont_extend_width=True
    )
    footer_window = Window(
        content=FormattedTextControl(text=[('ansibrightblack', '[F3] Comment  [Enter] Select  [Ctrl+C] Cancel')]),
        height=1,
    )
    container = Frame(
        HSplit(
            [
                results_window,
                Window(height=1, char='─', style='class:line'),
                VSplit([label_window, search_window]),
                footer_window,
            ]
        ),
        title='History Search',
    )
    layout = Layout(container)
    app = Application(layout=layout, key_bindings=kb, full_screen=True, erase_when_done=False)
    result = await app.run_async()
    if result:
        event.current_buffer.text = result
        event.current_buffer.cursor_position = len(result)
