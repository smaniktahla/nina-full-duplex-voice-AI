# Voice timer

Say "set a timer for fifteen minutes". Nina confirms and asks whether to label it.
Within 60 seconds, reply "sure, this is for the turkey baking in the oven" or
"yeah, for the kids reading before bedtime". "Name the timer turkey" also works later.
Ask "how much time is left on the timer?" to hear the remaining time and reopen
the countdown. "Cancel the timer" clears it.

Durations support seconds, minutes, hours, combined units and common number words,
from 1 second to 24 hours. Only one timer runs at a time; a second request asks
the user to cancel the existing timer first. Multiple timers are future work.

Creation, naming, status and expiry reuse the agenda viewer and its
`display_ops.show_agenda` / `script.dashboard_show` interruption path, including
route pin protection, offline checks, verified delivery, and bounded popup holds.
No additional Home Assistant dashboard is needed. The countdown continues when
dashboard rotation resumes. Expiry opens a "Time is up!" popup. This version has
no audible alarm. A Nina process restart clears timer state.

`live/timers.py` owns the process-local monotonic deadline. The signed agenda
document endpoint supplies current remaining time on reload; the viewer counts
locally between fetches. Popup failure does not prevent a timer being set.

Validation:

```sh
python -m pytest tests/test_timers.py tests/test_display_wiring.py tests/test_kiosk.py tests/test_routing_corpus.py needle-voice/tests/test_display_ops.py needle-voice/tests/test_executor.py -q
python kiosk-agenda/test_agenda.py
```

The viewer test requires Playwright and Chromium. Three existing tests require
POSIX date formatting/file mode semantics and fail on Windows.
