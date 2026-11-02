"""Shared formal/replay persistence defaults; durability is never optional."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ScanIOPolicyR25:
    capacity:int=32
    byte_capacity:int=64*1024**2
    group_size:int=8
    max_delay_s:float=.05

DEFAULT_IO_POLICY=ScanIOPolicyR25()

# R26 measured jitter option: the frozen default closes a group after 50 ms,
# which is shorter than one 6 Hz frame period, so the writer performs a
# payload+index fsync for nearly every frame. A 250 ms collection window still
# fsyncs every group and the complete drain, but admits 1-3 frames per fsync.
# This is opt-in: DEFAULT_IO_POLICY and the historical root07 binding stay
# unchanged until a paired board run proves the worst-5 s improvement.
BATCHED_REPLAY_IO_POLICY_R26=ScanIOPolicyR25(max_delay_s=.25)
