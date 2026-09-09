"""Native row actions and cancellable, asynchronous media-information dialogs."""

from editing import ROOT, method, source_method


def apply():
    path = ROOT / 'app/ui/file_queue.py'
    method(path, 'FileQueueRow', '_setup_context_menu', '''
    def _setup_context_menu(self):
        menu_model = Gio.Menu()
        menu_model.append(_("Delete File"), "row.delete")
        menu_model.append(_("Open Containing Folder"), "row.open_folder")
        menu_model.append(_("More Information..."), "row.info")
        self._menu = Gtk.PopoverMenu.new_from_model(menu_model)
        button = Gtk.MenuButton(icon_name="view-more-symbolic", popover=self._menu, valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.update_property([Gtk.AccessibleProperty.LABEL], [_("File options")])
        self.add_suffix(button)
        actions = Gio.SimpleActionGroup()
        self._delete_action = Gio.SimpleAction.new("delete", None)
        self._delete_action.connect("activate", lambda action, parameter: self.on_delete_callback(self.index, self.file_path))
        actions.add_action(self._delete_action)
        for name, callback in (("open_folder", self._on_open_folder), ("info", self._on_show_info)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        self.insert_action_group("row", actions)
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", lambda gesture, count, x, y: self._menu.popup())
        self.add_controller(gesture)
        self._info_requests = {}
    ''')
    method(path, 'FileQueueRow', '_on_open_folder', '''
    def _on_open_folder(self, action, param):
        source = getattr(self, "media_source", MediaSource.resolve(self.file_path))
        try:
            parent = Gio.File.new_for_path(source.path).get_parent()
            Gio.AppInfo.launch_default_for_uri(parent.get_uri(), None)
        except Exception as error:
            owner = self.queue_owner() if hasattr(self, "queue_owner") else None
            if owner and owner._parent_window:
                owner._parent_window._show_error_dialog(_("Could not open the folder"), str(error))
    ''')
    method(path, 'FileQueueRow', '_on_show_info', '''
    def _on_show_info(self, action, param):
        owner = self.queue_owner() if hasattr(self, "queue_owner") else None
        if owner is None or owner._disposed:
            return
        source = self.media_source
        dialog = Adw.Dialog(title=_("File Information"), content_width=640, content_height=540)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        copy_button = Gtk.Button(icon_name="edit-copy-symbolic", sensitive=False)
        copy_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Copy information to clipboard")])
        header.pack_end(copy_button)
        toolbar.add_top_bar(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_start=16, margin_end=16, margin_top=12, margin_bottom=16)
        content.append(Gtk.Label(label=source.path, xalign=0, wrap=True, selectable=True))
        loading = Gtk.Label(label=_("Analyzing audio…"), xalign=0)
        content.append(loading)
        scrolled = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_child(content)
        toolbar.set_content(scrolled)
        dialog.set_child(toolbar)
        request_id = uuid.uuid4().hex
        self._info_requests[request_id] = dialog
        def closed(dialog):
            self._info_requests.pop(request_id, None)
            owner._media_tasks.cancel(request_id)
        dialog.connect("closed", closed)
        def completed(info, error):
            if request_id not in self._info_requests or owner._disposed:
                return
            content.remove(loading)
            if error or info is None:
                content.append(Gtk.Label(label=_("Media information is unavailable. Check that the file still exists and is readable."), wrap=True))
                return
            try:
                stream = audio_stream(info, source.stream_index)
                properties = self._extract_audio_props(stream, info, source.stream_index is not None, source.path)
                content.append(self._create_info_group(_("Audio Properties"), properties))
                tags = dict(info.get("format", {}).get("tags", {}))
                tags.update(stream.get("tags", {}))
                items = [(str(key)[:128], str(value)[:2048]) for key, value in list(tags.items())[:200]]
                if items:
                    content.append(self._create_info_group(_("Metadata Tags"), items))
                if len(tags) > 200 or any(len(str(value)) > 2048 for value in tags.values()):
                    content.append(Gtk.Label(label=_("Long metadata is shortened for display. Copy retains the complete values."), wrap=True))
                clipboard_text = source.path + "\\n" + "\\n".join(f"{key}: {value}" for key, value in properties)
                clipboard_text += "\\n" + "\\n".join(f"{key}: {value}" for key, value in tags.items())
                copy_button.connect("clicked", lambda button: Gdk.Display.get_default().get_clipboard().set(clipboard_text))
                copy_button.set_sensitive(True)
            except (ValueError, TypeError, KeyError) as error:
                content.append(Gtk.Label(label=_("No readable audio stream was found."), wrap=True))
        dialog.present(owner._parent_window)
        owner._media_tasks.submit(request_id, source, completed)
    ''')
    method(path, 'FileQueueRow', 'cleanup', '''
    def cleanup(self):
        owner = self.queue_owner() if hasattr(self, "queue_owner") else None
        for identity, dialog in list(self._info_requests.items()):
            if owner is not None:
                owner._media_tasks.cancel(identity)
            dialog.close()
        self._info_requests.clear()
        self._menu.popdown()
    ''')
    create = source_method(path, 'FileQueue', '_create_media_row')
    create = create.replace('    row.media_source = source', '    row.media_source = source\n    row._delete_action.set_enabled(source.stream_index is None)')
    method(path, 'FileQueue', '_create_media_row', create)
