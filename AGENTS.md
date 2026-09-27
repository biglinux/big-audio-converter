# Resource lifetime contracts

Run GUI checks only in an isolated session. The focused native-finalization gate is:

```sh
/note/bigdesktop/scripts/headless-gate.sh --accessible python -m pytest -q tests/test_resource_lifetimes.py
```

A callback owned by a child must not retain its dialog or widget ancestor.
Use the signal's widget to find its ancestor at invocation, or weak references
when the owner is retained independently. Capture configuration directly instead
of capturing a wrapper that owns the entire dialog. Qdata destructors, rather
than weak notifications at dispose, prove that native widgets finalized.
