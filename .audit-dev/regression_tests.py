"""Update an obsolete omission expectation and declare required packages."""

from pathlib import Path
from editing import method, write


def apply():
    method(Path("tests/test_audio_converter.py"), "TestBuildAudioFilters", "test_noise_reduction_without_ladspa", '''
    def test_noise_reduction_without_ladspa(self, converter):
        converter.gtcrn_ladspa_path = None
        with pytest.raises(ValueError, match="unavailable"):
            converter._build_audio_filters({"noise_reduction": True})
    ''')
    package = Path("pkgbuild/PKGBUILD")
    text = package.read_text()
    if "'python-numpy'" not in text:
        text = text.replace("'python-cairo'", "'python-cairo' 'python-numpy'")
    text = text.replace("#makedepends=('')", "makedepends=('git' 'gettext')")
    package.write_text(text)
    write("docs/AUDIT_REMEDIATION.md", '''
    # Audit remediation

    Audit baseline: `95f02c3a019af7cb40813fa38af25c7b3906c99c`.

    This feature branch is a work in progress, not a statement that all audit
    findings have been closed. Each runtime claim must name its tested commit.

    The first change group adds private same-filesystem output staging,
    no-clobber publication, strict segment validation, shared cancellable process
    ownership, bounded pipe draining, explicit codec/PCM/sample-rate policies,
    metadata mapping, and real FFmpeg regression tests. Existing GUI callback
    arguments are retained; structured results additionally record actual output
    paths and per-file states. NumPy and build-tool dependencies are declared.

    ## Remaining acceptance work

    Waveform accuracy and lifecycle, native player shutdown, asynchronous UI
    probes, keyboard-accessible editing, adaptive layout, contextual tooltips,
    full gettext synchronization, packaging reproducibility, desktop integration,
    long-session resource measurements, and the release/translation pipeline
    require separate verification. The conflicting MIT/GPL license declarations
    require a maintainer decision; this branch must not silently relicense code.

    ## Output policy

    Existing files and dangling symlinks are never intentionally replaced.
    Export names can acquire a numeric suffix; consumers must use `FileResult.outputs`
    rather than reconstructing names. A multi-output transaction validates every
    output before publication and rolls back its own publications on failure.
    It is not a filesystem-wide atomic commit or a power-loss recovery journal.

    Stream-copy trimming remains packet-accurate, not sample-accurate. The codec
    bitstream is not re-encoded, but container metadata capabilities still differ.
    Precision and output-rate policies are tested against decoded PCM/FFprobe.
    ''')
