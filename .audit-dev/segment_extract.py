"""Apply independent trimming, filtering and encoding in the segment pipeline."""

from editing import ROOT, method


def apply():
    path = ROOT / "app/audio/segment_processor.py"
    method(path, "SegmentProcessor", "_extract_segment", '''
    def _extract_segment(self, input_file, segment, output_file, audio_filters=None,
                         track_index=None, codec_params=None):
        """Extract into a private file, then publish without replacing anything."""
        try:
            interval = Segment.from_mapping(segment)
            with OutputTransaction([output_file], rename_on_conflict=False) as transaction:
                staged = transaction.staged[0]
                command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-y",
                           "-ss", f"{interval.start:.9f}", "-t", f"{interval.duration:.9f}",
                           "-i", os.path.abspath(input_file),
                           "-map", f"0:{track_index}" if track_index is not None else "0:a:0",
                           "-map_metadata", "0"]
                source_info = getattr(self, "source_info", {})
                command.extend(artwork_args(source_info, output_file))
                if codec_params or audio_filters:
                    chain = [f"atrim=duration={interval.duration:.9f}", "asetpts=PTS-STARTPTS"]
                    if audio_filters:
                        chain.append(audio_filters)
                    command.extend(["-af", ",".join(chain)])
                    if codec_params:
                        command.extend(codec_params)
                else:
                    extension = os.path.splitext(output_file)[1].lstrip(".").lower()
                    command.extend(["-c:a", "copy", "-f", COPY_MUXERS.get(extension, extension)])
                command.append(staged)
                result = self.runner.run(command)
                if result.returncode:
                    logger.error("Segment extraction failed: %s", result.stderr)
                    return False
                if not self._output_has_audio(staged):
                    return False
                transaction.commit()
            return True
        except (OSError, ValueError, subprocess.SubprocessError, OperationCancelled) as error:
            logger.error("Segment extraction did not complete: %s", error)
            return False
    ''')
    method(path, "SegmentProcessor", "_output_has_audio", '''
    def _output_has_audio(self, path):
        """Reject empty headers, including containers with substantial metadata."""
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        candidate = str(Path(self.ffmpeg_path).with_name("ffprobe"))
        ffprobe = candidate if os.path.isfile(candidate) else "ffprobe"
        result = self.runner.run([ffprobe, "-v", "error", "-select_streams", "a:0",
                                  "-show_entries", "stream=duration:format=duration", "-of", "json", path], timeout=10)
        if result.returncode:
            return False
        info = json.loads(result.stdout)
        if not info.get("streams"):
            return False
        durations = [s.get("duration") for s in info["streams"]] + [info.get("format", {}).get("duration")]
        for value in durations:
            if value not in (None, "N/A"):
                duration = float(value)
                if math.isfinite(duration) and duration > 0:
                    return True
        result = self.runner.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_packets",
                                  "-read_intervals", "%+#1", "-of", "json", path], timeout=10)
        return result.returncode == 0 and bool(json.loads(result.stdout).get("packets"))
    ''')
