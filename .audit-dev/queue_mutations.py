"""Keep row identities, selection and background tasks coherent during mutations."""

import ast
from editing import ROOT, method, source_method


def apply():
    path = ROOT / 'app/ui/file_queue.py'
    method(path, 'FileQueue', '_selected_identifiers', '''
    def _selected_identifiers(self):
        def identify(index):
            return self.files[index] if index is not None and 0 <= index < len(self.files) else None
        return identify(self.active_file_index), identify(self.currently_playing_index)
    ''')
    method(path, 'FileQueue', '_restore_identifiers', '''
    def _restore_identifiers(self, active, playing):
        for index, row in enumerate(self.file_rows):
            row.index = index
        self.active_file_index = self.files.index(active) if active in self.files else None
        self.currently_playing_index = self.files.index(playing) if playing in self.files else None
        window = self._parent_window
        if window is not None:
            window.current_file_index = self.files.index(window.current_file) if window.current_file in self.files else -1
    ''')
    method(path, 'FileQueue', 'move_file', '''
    def move_file(self, old_index, new_index):
        if not 0 <= old_index < len(self.files) or not 0 <= new_index < len(self.files):
            return False
        active, playing = self._selected_identifiers()
        row = self.file_rows.pop(old_index)
        identifier = self.files.pop(old_index)
        self.files.insert(new_index, identifier)
        self.file_rows.insert(new_index, row)
        self.file_list.remove(row)
        self.file_list.insert(row, new_index)
        row.remove_css_class("drag-row")
        self._restore_identifiers(active, playing)
        return True
    ''')
    source = source_method(path, 'FileQueue', '_on_row_drop')
    first = source.index('        # Determine if we')
    last = source.index('    except Exception', first)
    source = source[:first] + '        return self.move_file(old_index, min(new_index, len(self.files) - 1))\n\n' + source[last:]
    method(path, 'FileQueue', '_on_row_drop', source)
    method(path, 'FileQueue', 'remove_file', '''
    def remove_file(self, index):
        if not 0 <= index < len(self.files):
            return False
        active, playing = self._selected_identifiers()
        row = self.file_rows[index]
        identifier = row.file_path
        self._media_tasks.cancel(row.request_id)
        self._rows_by_id.pop(row.request_id, None)
        self.track_metadata.pop(identifier, None)
        if identifier == playing and callable(self.on_stop_playback):
            self.on_stop_playback()
        if identifier in (active, playing) and callable(self.on_playing_file_removed):
            self.on_playing_file_removed()
        self.files.pop(index)
        self.file_rows.pop(index)
        self.file_list.remove(row)
        row.cleanup()
        self._restore_identifiers(active, playing)
        self.update_queue_size_label()
        if self.file_removed_signal:
            self.file_removed_signal(identifier)
        return True
    ''')
    method(path, 'FileQueue', 'clear_queue', '''
    def clear_queue(self):
        if callable(self.on_stop_playback):
            self.on_stop_playback()
        identifiers = list(self.files)
        for row in self.file_rows:
            self._media_tasks.cancel(row.request_id)
            row.cleanup()
            self.file_list.remove(row)
        self.files.clear()
        self.file_rows.clear()
        self._rows_by_id.clear()
        self.track_metadata.clear()
        self.currently_playing_index = None
        self.active_file_index = None
        if not self._disposed:
            self.update_queue_size_label()
            for identifier in identifiers:
                if self.file_removed_signal:
                    self.file_removed_signal(identifier)
    ''')
    method(path, 'FileQueue', 'cleanup', '''
    def cleanup(self):
        if self._disposed:
            return
        self._disposed = True
        self._media_tasks.close()
        self._sources.close()
        self.clear_queue()
        self._parent_window = None
        self._tooltip_helper = None
    ''')
    method(path, 'FileQueue', '_on_delete_response', '''
    def _on_delete_response(self, dialog, response, index, file_path):
        if response != "delete" or file_path not in self.files:
            return
        current = self.files.index(file_path)
        row = self.file_rows[current]
        if row.media_source.stream_index is not None:
            return
        try:
            os.remove(row.media_source.path)
            self.remove_file(current)
        except OSError as error:
            if self._parent_window is not None:
                self._parent_window._show_error_dialog(_("Error Deleting File"), str(error))
    ''')
    source = source_method(path, 'FileQueue', 'update_progress')
    source = source.replace('GLib.timeout_add(1500, hide_progress)', 'self._sources.timeout(1500, hide_progress)')
    source = source.replace('                row.progress_bar.set_visible(False)', '                if row.request_id in self._rows_by_id:\n                    row.progress_bar.set_visible(False)')
    method(path, 'FileQueue', 'update_progress', source)
    # Remove the now-unused synchronous/position-indexed metadata pipeline.
    text = path.read_text()
    tree = ast.parse(text)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'FileQueue')
    lines = text.splitlines(keepends=True)
    obsolete = {'_get_audio_tracks', '_get_track_duration', '_add_track_entry', '_start_metadata_thread', '_process_metadata_queue'}
    for node in reversed(cls.body):
        if isinstance(node, ast.FunctionDef) and node.name in obsolete:
            del lines[node.lineno - 1:node.end_lineno]
    text = ''.join(lines)
    text = '\n'.join(line for line in text.splitlines() if not any(anchor in line for anchor in ('self._metadata_queue =', 'self._metadata_thread ='))) + '\n'
    path.write_text(text)
