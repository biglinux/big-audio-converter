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

Segment-row callbacks must not capture the row dictionary: it owns the buttons
that own the callbacks. Resolve the current row from the emitting button.

Queue row menus and popup factories follow the row lifetime. Their callbacks
must hold the row weakly, including lazy menus created after construction.

Drawing functions receive their area; controllers expose their widget. Use
those arguments rather than bound methods that keep the drawing area alive.

Conversion dialogs must release their controller reference on close. Repeated
bound-method signal connections use `weak_callback`; retain the returned handler
ID when a control temporarily blocks that connection.

A closed main window must release application actions, the active-window slot,
queue parent references and manually parented popovers. Test window finalization
while the application remains alive so process exit cannot hide survivors.

Persistent worker loops must drop the completed request and its payload before
waiting for another job. Otherwise the thread retains the last callback owner,
metadata or marker snapshot even after main-loop delivery and cancellation.
