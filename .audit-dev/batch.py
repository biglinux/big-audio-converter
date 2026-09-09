"""Keep structured per-file results while preserving the existing GTK callback API."""

from editing import ROOT, method


def apply():
    path = ROOT / "app/audio/converter.py"
    method(path, "AudioConverter", "_dispatch", '''
    @staticmethod
    def _dispatch(callback, *args):
        if callback is None:
            return
        def invoke():
            callback(*args)
            return False
        GLib.idle_add(invoke)
    ''')
    method(path, "AudioConverter", "convert_all_files", '''
    def convert_all_files(self, files, settings, progress_callback, finish_callback):
        """Process a stable request snapshot and report every input's final state."""
        import copy
        files = list(files)
        self.last_batch = BatchResult()
        try:
            self.reset_cancellation()
            snapshot = copy.deepcopy(settings)
            for index, identifier in enumerate(files):
                if self.cancel_flag:
                    self.last_batch.files.extend(FileResult(path, "cancelled", message="Not processed because the batch was cancelled.") for path in files[index:])
                    break
                output = self._get_output_path(identifier, snapshot["format"])
                def progress(value, index=index, identifier=identifier):
                    self._dispatch(progress_callback, index, identifier, value)
                self.convert_file(identifier, output, snapshot, progress)
                self.last_batch.files.append(self.last_result)
            succeeded = self.last_batch.successful_sources
            failed = len(self.last_batch.failed_sources)
            cancelled = sum(item.status == "cancelled" for item in self.last_batch.files)
            message = f"{len(succeeded)} completed; {failed} failed; {cancelled} cancelled."
            self._dispatch(finish_callback, bool(succeeded), message, succeeded)
        except Exception as error:
            # This is the worker's presentation boundary. Preserve diagnostics
            # while ensuring an unexpected error cannot strand a progress dialog.
            logger.exception("Batch conversion failed")
            self._dispatch(finish_callback, False, str(error), self.last_batch.successful_sources)
    ''')
