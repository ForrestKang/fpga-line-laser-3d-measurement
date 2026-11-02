"""R19 snapshot policy; the frozen reconstruction and admission stay unchanged."""
from __future__ import annotations

import numpy as np

from .estimator import VirtualScanEstimator


class PipelineEstimator(VirtualScanEstimator):
    """Keep every observed point, concatenating only on an explicit snapshot.

    ``process_stripe_only`` returns the unchanged VirtualProfile. The inherited
    process/process_stripe APIs still return full immutable cloud snapshots.
    """

    def __init__(self, config):
        super().__init__(config)
        self._defer_snapshot = False
        self._snapshot_revision = -1
        self._snapshot_cache = np.empty((0, 3), float)
        self._snapshot_cache.setflags(write=False)
        self.snapshot_build_count = 0

    def process_stripe_only(self, center_q12, stripe_accepted, stripe_reasons, metadata):
        self._defer_snapshot = True
        try:
            profile, _ = super().process_stripe(center_q12, stripe_accepted,
                                              stripe_reasons, metadata)
            return profile
        finally:
            self._defer_snapshot = False

    def get_cloud(self):
        if self._defer_snapshot:
            return self._snapshot_cache
        if self._snapshot_revision != self._revision:
            cloud = np.concatenate(self._cloud) if self._cloud else np.empty((0, 3), float)
            cloud.setflags(write=False)
            self._snapshot_cache = cloud
            self._snapshot_revision = self._revision
            self.snapshot_build_count += 1
        return self._snapshot_cache
