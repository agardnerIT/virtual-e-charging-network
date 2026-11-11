"""The device's own clock, and the one knob that makes it wrong.

Every device here carries a clock that is deliberately skewed (plan.md D12, 5.5): a rural
meter is 7 s behind, a solar inverter 4 s ahead, an island 11 s behind. That is enough to
make the dual-timestamp story visible. It is not enough to break causality, which is what
issue #4 asks for - so this module adds a *runtime* skew control: the same device can be
pushed minutes out without restarting it, which is what a clock that has drifted or been
set by hand actually does.

Two things follow from a device clock that is minutes out, and both are the point:

* **A clock that is behind makes every point look `stale`.** The poller compares the
  device's own clock against its own (`STALE_AFTER_S`) - so a healthy device with a wrong
  clock reports `otel.data_point.quality = stale` on every point. That is the poller doing
  exactly what it was told. It is *not* `comms-lost`, and the link is fine: the edge queue
  is empty, the samples keep arriving, and the receive timestamp is seconds old. The words
  in the quality vocabulary mean different things and one of them is being misused by the
  data, which is a finding, not a bug to paper over.
* **A clock that is ahead makes every point look `good`.** The age is negative, no threshold
  trips, and the timestamps are in the future. Nothing complains. This is the dangerous
  direction, and it is why the answer is "never order by device time" rather than "reject
  stale data".

The device never corrects itself and nothing downstream rewrites the value (D23). Provenance
that gets "fixed" is not provenance.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class ClockState:
    """Thread-safe holder for the device's clock offset, in seconds.

    ``skew_s`` is what the device adds to the real time to produce its own reading. A
    negative value means the device believes it is earlier than it is.
    """

    initial_skew_s: float = 0.0
    skew_s: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.skew_s = self.initial_skew_s

    def set_skew(self, seconds: float) -> dict:
        with self.lock:
            self.skew_s = float(seconds)
            return self.snapshot_locked()

    def clear(self) -> dict:
        with self.lock:
            self.skew_s = self.initial_skew_s
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict:
        return {
            "clock_skew_s": round(self.skew_s, 1),
            "device_time": int(time.time() + self.skew_s),
            "device_time_note": ("behind real time" if self.skew_s < 0
                                 else "ahead of real time" if self.skew_s > 0
                                 else "aligned"),
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self.snapshot_locked()

    def now(self) -> int:
        with self.lock:
            return int(time.time() + self.skew_s)
