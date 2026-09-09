"""Replace blocking subprocess ownership with the common cancellable runner."""

from editing import ROOT, method


def apply():
    path = ROOT / "app/audio/converter.py"
    method(path, "AudioConverter", "_find_ffmpeg", '''
    def _find_ffmpeg(self):
        """Discover executables without launching a process during GTK startup."""
        found = shutil.which("ffmpeg")
        if found:
            return found
        for candidate in ("/usr/lib/jellyfin-ffmpeg/ffmpeg", "/opt/local/bin/ffmpeg"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None
    ''')
    method(path, "AudioConverter", "_get_runner", '''
    def _get_runner(self):
        if not hasattr(self, "_runner"):
            self._runner = ProcessRunner(lambda: self.cancel_flag)
        return self._runner
    ''')
    method(path, "AudioConverter", "_probe_media", '''
    def _probe_media(self, path):
        """Read bounded JSON metadata with a cancellable ten-second deadline."""
        candidate = str(Path(self.ffmpeg_path or "ffmpeg").with_name("ffprobe"))
        ffprobe = candidate if os.path.isfile(candidate) else shutil.which("ffprobe")
        if not ffprobe:
            raise ValueError("FFprobe is not installed")
        result = self._get_runner().run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", os.path.abspath(path)],
            timeout=10,
        )
        if result.returncode:
            self.last_diagnostics = result.stderr
            raise ValueError("The media could not be read. It may be damaged or unsupported.")
        return json.loads(result.stdout)
    ''')
    method(path, "AudioConverter", "_run_ffmpeg", '''
    def _run_ffmpeg(self, command, duration=0, progress_callback=None):
        """Drain both pipes even when duration or a progress callback is absent."""
        pending = bytearray()
        def consume(data):
            pending.extend(data)
            while b"\\n" in pending:
                line, _, rest = pending.partition(b"\\n")
                pending[:] = rest
                if line.startswith(b"out_time_us=") and duration and progress_callback:
                    try:
                        position = float(line.partition(b"=")[2]) / 1_000_000
                    except ValueError:
                        continue
                    progress_callback(max(0.0, min(position / duration, 0.99)))
            if len(pending) > 8192:
                raise ValueError("Invalid FFmpeg progress stream")
        cmd = command[:1] + ["-hide_banner", "-nostdin", "-loglevel", "error", "-xerror", "-nostats", "-progress", "pipe:1"] + command[1:]
        result = self._get_runner().run(cmd, stdout_consumer=consume)
        self.last_diagnostics = result.stderr
        return result.returncode == 0 and not self.cancel_flag
    ''')
    method(path, "AudioConverter", "_get_duration", '''
    def _get_duration(self, file_path):
        try:
            info = self._probe_media(file_path)
            value = float(info.get("format", {}).get("duration", 0))
            return value if math.isfinite(value) and value > 0 else 0
        except (OSError, ValueError, subprocess.SubprocessError, OperationCancelled):
            return 0
    ''')
    method(path, "AudioConverter", "cancel_conversion", '''
    def cancel_conversion(self):
        """Request cancellation; the worker retains ownership until it reaps the child."""
        self.cancel_flag = True
        runner = getattr(self, "_runner", None)
        if runner is not None:
            runner.cancel()
    ''')
    method(path, "AudioConverter", "reset_cancellation", '''
    def reset_cancellation(self):
        """Start a new operation only after the previous process has finished."""
        self._get_runner().reset()
        self.cancel_flag = False
    ''')
    method(path, "AudioConverter", "_build_codec_args", '''
    def _build_codec_args(self, settings, channels=None):
        return build_codec_args(settings, channels, settings.get("_source_stream"))
    ''')
