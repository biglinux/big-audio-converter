"""Transactional conversion boundary, kept separate for review."""

from editing import ROOT, method


def apply():
    path = ROOT / "app/audio/converter.py"
    method(path, "AudioConverter", "convert_file", '''
    def convert_file(self, input_path, output_path, settings, progress_callback=None):
        """Convert an immutable request and publish only validated complete outputs."""
        identifier = os.fspath(input_path)
        options = dict(settings)
        self.last_diagnostics = ""
        try:
            if self.cancel_flag:
                raise OperationCancelled()
            if not self.ffmpeg_path:
                raise ValueError("FFmpeg is not installed")
            if not os.fspath(output_path) or "\\x00" in os.fspath(output_path):
                raise ValueError("A valid output filename is required")
            source = MediaSource.resolve(identifier, options.get("track_metadata"))
            if not os.path.isfile(source.path):
                raise ValueError("The input is not a readable local file")
            info = self._probe_media(source.path)
            streams = [stream for stream in info.get("streams", [])
                       if stream.get("codec_type") == "audio"
                       and (source.stream_index is None or stream["index"] == source.stream_index)]
            if not streams:
                raise ValueError("The selected file or stream does not contain audio")
            stream = streams[0]
            options["_source_stream"] = stream
            options["_source_probe"] = info
            value = stream.get("duration") or info.get("format", {}).get("duration")
            duration = float(value) if value not in (None, "N/A") else None
            if duration is not None and (not math.isfinite(duration) or duration <= 0):
                duration = None
            raw = []
            if options.get("cut_enabled"):
                raw = options.get("file_markers", {}).get(identifier, options.get("cut_segments", []))
            if not isinstance(raw, (list, tuple)) or len(raw) > 256:
                raise ValueError("Provide at most 256 valid segments")
            segments = [Segment.from_mapping(item, duration).as_dict() for item in raw]
            separate = bool(segments) and not options.get("cut_merge", True) and len(segments) > 1
            base, extension = os.path.splitext(os.fspath(output_path))
            destinations = ([f"{base}_segment{index + 1}{extension}" for index in range(len(segments))]
                            if separate else [os.fspath(output_path)])
            warnings = []
            fmt = options.get("format", "mp3")
            if fmt == "copy" and segments:
                warnings.append("Stream-copy cuts are approximate and follow codec packet boundaries.")
            if fmt != "copy" and output_rate(options, stream) != int(stream.get("sample_rate", 48000)):
                warnings.append("The output codec requires a different sample rate.")
            if fmt == "copy":
                artwork_format = extension.lstrip(".").lower()
            else:
                artwork_format = fmt
            if any(s.get("disposition", {}).get("attached_pic") for s in info.get("streams", [])) and artwork_format not in ARTWORK_FORMATS:
                warnings.append("This output container does not preserve the embedded cover image.")
            with OutputTransaction(destinations) as transaction:
                for index, staged in enumerate(transaction.staged):
                    current = dict(options)
                    current_segments = [segments[index]] if separate else segments
                    current["file_markers"] = {identifier: current_segments}
                    current["cut_merge"] = True
                    def progress(value, index=index):
                        if progress_callback is not None:
                            progress_callback(min(0.99, (index + value) / len(destinations)))
                    if not self._convert_file_staged(identifier, staged, current, progress):
                        if self.cancel_flag:
                            raise OperationCancelled()
                        raise RuntimeError("Audio processing failed. Check the format, write permission and free disk space.")
                    output_info = self._probe_media(staged)
                    if not any(s.get("codec_type") == "audio" for s in output_info.get("streams", [])) or os.path.getsize(staged) == 0:
                        raise RuntimeError("The encoder did not produce a valid audio output")
                if self.cancel_flag:
                    raise OperationCancelled()
                outputs = transaction.commit()
            self.last_result = FileResult(identifier, "success", outputs, warnings=tuple(warnings))
            if progress_callback is not None:
                progress_callback(1.0)
            return True
        except OperationCancelled:
            self.last_result = FileResult(identifier, "cancelled", message="Conversion cancelled; unfinished outputs were removed.")
            return False
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            self.last_result = FileResult(identifier, "failed", message=str(error), diagnostics=self.last_diagnostics)
            logger.error("Conversion failed: %s", error)
            return False
    ''')
