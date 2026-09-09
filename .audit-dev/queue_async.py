"""Move all queue probing to one cancellable worker with stable row identities."""

from editing import ROOT, method, source_method


def apply():
    path = ROOT / 'app/ui/file_queue.py'
    text = path.read_text()
    text = text.replace('import time\n', 'import time\nimport uuid\nimport weakref\nfrom app.audio.models import MediaSource\nfrom app.audio.media_probe import audio_stream, media_duration\nfrom app.audio.media_tasks import MediaTasks\nfrom app.utils.main_context import SourceGroup\n')
    path.write_text(text)
    init = source_method(path, 'FileQueue', '__init__')
    init += '''\n    self._disposed = False
    self._sources = SourceGroup()
    self._media_tasks = MediaTasks(self.converter.ffmpeg_path)
    self._rows_by_id = {}
'''
    method(path, 'FileQueue', '__init__', init)
    method(path, 'FileQueue', '_create_media_row', '''
    def _create_media_row(self, identifier, source, index=None):
        if index is None:
            index = len(self.files)
        row = FileQueueRow(identifier, index, self.on_remove_file, self.on_play_file,
                           self.on_delete_file, self.on_activate_file)
        row.request_id = uuid.uuid4().hex
        row.media_source = source
        row.queue_owner = weakref.ref(self)
        row.metadata_ready = False
        row.set_activatable(False)
        row.play_button.set_sensitive(False)
        row.set_metadata(_("Analyzing audio…"))
        self.files.insert(index, identifier)
        self.file_rows.insert(index, row)
        self._rows_by_id[row.request_id] = row
        self.file_list.insert(row, index)
        self._apply_row_tooltips(row)
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", self._on_row_drag_prepare, row)
        drag.connect("drag-begin", self._on_row_drag_begin, row)
        row.add_controller(drag)
        for position, item in enumerate(self.file_rows):
            item.index = position
        return row
    ''')
    method(path, 'FileQueue', 'add_file', '''
    def add_file(self, file_path):
        """Display immediately; never run a decoder from a GTK event handler."""
        if self._disposed or len(self.files) >= 4096:
            return False
        try:
            source = MediaSource.resolve(file_path)
            if not os.path.isfile(source.path) or not self._is_valid_media_file_quick(source.path):
                return False
            if any(row.media_source.path == source.path for row in self.file_rows):
                return False
            row = self._create_media_row(source.path, source)
            self._media_tasks.submit(row.request_id, source,
                lambda info, error, identity=row.request_id: self._metadata_ready(identity, info, error))
            if not self._updates_suspended:
                self.update_queue_size_label()
            return True
        except (OSError, ValueError, TypeError) as error:
            logger.warning("The media could not be queued: %s", error)
            return False
    ''')
    method(path, 'FileQueue', '_fill_media_row', '''
    def _fill_media_row(self, row, info, stream):
        row.metadata_ready = True
        row.set_activatable(True)
        row.play_button.set_sensitive(True)
        duration = media_duration(info, stream)
        parts = [stream.get("codec_name", "").upper()]
        if duration:
            parts.append(FileQueueRow._format_duration(duration))
        if stream.get("sample_rate"):
            parts.append(_("{rate} Hz").format(rate=int(stream["sample_rate"])))
        channels = int(stream.get("channels", 0))
        parts.append(gettext.ngettext("{count} channel", "{count} channels", channels).format(count=channels))
        tags = stream.get("tags", {})
        if tags.get("language"):
            parts.append(str(tags["language"])[:32])
        if tags.get("title"):
            parts.append(str(tags["title"])[:160])
        row.set_metadata(" · ".join(parts))
    ''')
    method(path, 'FileQueue', '_metadata_ready', '''
    def _metadata_ready(self, identity, info, error):
        row = self._rows_by_id.get(identity)
        if self._disposed or row is None:
            return
        streams = [stream for stream in (info or {}).get("streams", []) if stream.get("codec_type") == "audio"]
        if error or not streams or len(streams) > 256:
            row.metadata_ready = True
            row.set_metadata(_("Audio unavailable. Check the file or remove it from the queue."))
            self.update_queue_size_label()
            return
        if len(streams) > 1:
            active = self.files[self.active_file_index] if self.active_file_index is not None else None
            playing = self.files[self.currently_playing_index] if self.currently_playing_index is not None else None
            index = self.file_rows.index(row)
            self._rows_by_id.pop(identity)
            self.file_rows.pop(index)
            self.files.pop(index)
            self.file_list.remove(row)
            row.cleanup()
            for offset, stream in enumerate(streams):
                extension = self._get_audio_codec_extension(stream.get("codec_name", ""))
                identifier = f"{row.media_source.path}::track{offset + 1}{extension}"
                source = MediaSource(row.media_source.path, stream["index"])
                self.track_metadata[identifier] = dict(source_video=source.path, track_index=source.stream_index,
                    codec=stream.get("codec_name", ""), channels=stream.get("channels", 0),
                    sample_rate=stream.get("sample_rate", ""), bitrate=stream.get("bit_rate", ""),
                    language=str(stream.get("tags", {}).get("language", ""))[:32],
                    title=str(stream.get("tags", {}).get("title", ""))[:160])
                item = self._create_media_row(identifier, source, index + offset)
                name = _("{name} — audio track {number}").format(name=os.path.basename(source.path), number=offset + 1)
                item.set_title(GLib.markup_escape_text(name))
                self._fill_media_row(item, info, stream)
            self.active_file_index = self.files.index(active) if active in self.files else None
            self.currently_playing_index = self.files.index(playing) if playing in self.files else None
        else:
            self._fill_media_row(row, info, streams[0])
        self.update_queue_size_label()
        if self.active_file_index is None and self.file_rows and self.file_rows[0].play_button.get_sensitive():
            first = self.file_rows[0]
            if self.on_file_added_to_empty_queue:
                self.on_file_added_to_empty_queue(first.file_path, 0)
    ''')
    method(path, 'FileQueue', 'get_queue_size_text', '''
    def get_queue_size_text(self):
        count = len(self.files)
        return gettext.ngettext("{count} file", "{count} files", count).format(count=count)
    ''')
    method(path, 'FileQueue', 'update_queue_size_label', '''
    def update_queue_size_label(self):
        text = self.get_queue_size_text()
        self.queue_size_label.set_text(text)
        if callable(getattr(self, "on_queue_size_changed", None)):
            self.on_queue_size_changed(len(self.files), text)
        window = self._parent_window
        if window is not None:
            pending = any(not row.metadata_ready for row in self.file_rows)
            session = getattr(window, "conversion_session", None)
            window.convert_button.set_sensitive(not pending and not (session and session.running))
    ''')
    # Unknown streams are put in Matroska rather than mislabeled as AAC.
    text = path.read_text().replace('codec_map.get(codec_name.lower(), ".aac")', 'codec_map.get(codec_name.lower(), ".mka")')
    text = text.replace('f"{int(audio_stream[\'sample_rate\']) // 1000} kHz"', '_("{rate} Hz").format(rate=int(audio_stream["sample_rate"]))')
    path.write_text(text)
