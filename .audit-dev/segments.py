"""Make segment requests complete-or-fail and share process ownership."""

from editing import ROOT, method, source_method
import segment_extract


def apply():
    path = ROOT / "app/audio/segment_processor.py"
    text = path.read_text()
    if 'from .models import Segment' not in text:
        text = text.replace('import logging\n', 'import logging\nimport json\nimport math\nfrom pathlib import Path\nimport tempfile\n\nfrom .models import Segment, format_timestamp\nfrom .process_runner import ProcessRunner, OperationCancelled\nfrom .output_transaction import OutputTransaction\nfrom .codec_profiles import artwork_args, COPY_MUXERS\n')
        path.write_text(text)
    method(path, "SegmentProcessor", "__init__", '''
    def __init__(self, ffmpeg_path, runner=None):
        self.ffmpeg_path = ffmpeg_path
        self.runner = runner if runner is not None else ProcessRunner()
        self.source_info = {}
    ''')
    method(path, "SegmentProcessor", "_validate_segments", '''
    def _validate_segments(self, segments):
        """Normalize valid intervals; callers must reject an incomplete result."""
        if not isinstance(segments, (list, tuple)) or len(segments) > 256:
            return []
        valid = []
        for item in segments:
            try:
                valid.append(Segment.from_mapping(item).as_dict())
            except (AttributeError, TypeError, ValueError):
                continue
        return valid
    ''')
    method(path, "SegmentProcessor", "_format_time", '''
    def _format_time(self, seconds):
        return format_timestamp(seconds)
    ''')
    source = source_method(path, "SegmentProcessor", "process_segments")
    if 'len(valid_segments) != len(segments)' not in source:
        source = source.replace('    if not valid_segments:', '    if len(valid_segments) != len(segments):\n        logger.error("Every requested segment must be valid")\n        return None\n    if not valid_segments:', 1)
        source = source.replace('            logger.error(f"Failed to extract segment {i + 1}")', '            logger.error(f"Failed to extract segment {i + 1}")\n            return None')
        # Numeric timestamps are the source of truth; diagnostic strings are optional.
        source = source.replace("s['start_str']", "s.get('start_str', s.get('start'))")
        method(path, "SegmentProcessor", "process_segments", source)
    segment_extract.apply()
    method(path, "SegmentProcessor", "_concatenate_segments", '''
    def _concatenate_segments(self, segment_files, output_file):
        """Use private ASCII list entries, avoiding filename escaping ambiguities."""
        if not segment_files:
            return False
        try:
            with OutputTransaction([output_file], rename_on_conflict=False) as transaction:
                parent = os.path.dirname(transaction.staged[0])
                with tempfile.TemporaryDirectory(prefix="concat-", dir=parent) as directory:
                    entries = []
                    for index, source in enumerate(segment_files):
                        name = f"part{index:04d}"
                        os.symlink(os.path.abspath(source), os.path.join(directory, name))
                        entries.append(f"file '{name}'\\n")
                    playlist = os.path.join(directory, "list.txt")
                    Path(playlist).write_text("".join(entries), encoding="utf-8")
                    original = getattr(self, "source_file", segment_files[0])
                    command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-y",
                               "-f", "concat", "-safe", "1", "-i", playlist, "-i", os.path.abspath(original),
                               "-map", "0:a:0", "-map_metadata", "1", "-c:a", "copy"]
                    command.extend(artwork_args(self.source_info, output_file, input_index=1))
                    command.append(transaction.staged[0])
                    result = self.runner.run(command)
                    if result.returncode:
                        logger.error("Segment concatenation failed: %s", result.stderr)
                        return False
                    if not self._output_has_audio(transaction.staged[0]):
                        return False
                    transaction.commit()
            return True
        except (OSError, ValueError, subprocess.SubprocessError, OperationCancelled) as error:
            logger.error("Segment concatenation did not complete: %s", error)
            return False
    ''')
