"""Small integration invariants shared by the first remediation group."""

from editing import ROOT, method, source_method


def apply():
    converter = ROOT / "app/audio/converter.py"
    text = converter.read_text()
    text = text.replace("actual_input_path = input_path", "actual_input_path = os.path.abspath(input_path)")
    text = text.replace('actual_input_path = track_metadata["source_video"]', 'actual_input_path = os.path.abspath(track_metadata["source_video"])')
    converter.write_text(text)
    profile = ROOT / "app/audio/codec_profiles.py"
    text = profile.read_text()
    anchor = 'def build_codec_args(settings, channels=None, source=None):\n'
    if '    channels = settings.get("channels") if channels is None else channels' not in text:
        text = text.replace(anchor, anchor + '    channels = settings.get("channels") if channels is None else channels\n')
    profile.write_text(text)
    outputs = ROOT / "app/audio/output_transaction.py"
    source = source_method(outputs, "OutputTransaction", "commit")
    if 'os.chmod(staged, 0o600)' not in source:
        source = source.replace('        with open(staged, "rb") as stream:', '        os.chmod(staged, 0o600)\n        with open(staged, "rb") as stream:')
        method(outputs, "OutputTransaction", "commit", source)
