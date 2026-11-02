"""Explicit saved-real Raw + modeled SLC2 adapters for the existing replay.

Only compact decode/selection hooks differ from ContinuousProcessor. Geometry,
strict training bounds, quality, validity and display preview are shared with
the production processor/replay, without calibrating the assumed axis.
"""
from dataclasses import dataclass, fields
import time

import numpy as np

from ..p2.types import DiagnosticsSnapshot
from ..stripe_v1.candidates import extract_candidates
from ..stripe_v1.selection_v4 import SelectionConfigV4, select_candidates_v4
from .continuous_processor import ContinuousProcessor
from .pipeline import DEFAULT_SELECTION
from .processed_replay import ReplayEntry, PreparedReplay, prepare_manifest_replay
from .ridge_candidates import select_ridge_candidates
from .stream_capture import FeatureCapture
from .stream_ridge import decode_slc2, model_slc2

POLICIES = ('ridge_raw_v4', 'ridge_primary_v4', 'ridge_primary_v4_batch')


@dataclass(frozen=True)
class RidgeReplayEntry(ReplayEntry):
    @property
    def provenance(self):
        result = super().provenance
        result['modeled_slc2_sha256'] = result.pop('modeled_slc1_sha256')
        result.update(source='saved_real_raw_modeled_slc2', modeled_slc1=False,
                      modeled_slc2=True, rtl_dma_captured=False, measurement_eligible=False)
        return result

    def capture(self):
        packet = decode_slc2(self.payload, frame_width=752, frame_height=480,
                            request_id=self.request_id, capture_epoch=self.epoch)
        values = {field.name:0 for field in fields(DiagnosticsSnapshot) if field.name != 'rows'}
        values.update(width=752, height=480, epoch=self.epoch,
                      request_id=self.request_id, row_count=480, ready=True)
        return FeatureCapture(self.request_id, self.epoch, self.raw, self.raw_sha256,
            None, DiagnosticsSnapshot(**values), None, profile_payload=self.payload, compact_packet=packet)


@dataclass(frozen=True)
class RidgePreparedReplay(PreparedReplay):
    @property
    def provenance(self):
        result = super().provenance
        result.update(source='saved_real_raw_modeled_slc2', modeled_slc1=False,
                      modeled_slc2=True, rtl_dma_captured=False, measurement_eligible=False)
        return result


def prepare_ridge_manifest_replay(dataset, stripe_config):
    started = time.perf_counter()
    original = prepare_manifest_replay(dataset, stripe_config)
    entries = []
    for entry in original.entries:
        values = {field.name:getattr(entry, field.name) for field in fields(ReplayEntry)}
        values['payload'] = model_slc2(entry.raw, stripe_config,
            request_id=entry.request_id, capture_epoch=entry.epoch)
        entries.append(RidgeReplayEntry(**values))
    values = {field.name:getattr(original, field.name) for field in fields(PreparedReplay)}
    values.update(entries=tuple(entries), preparation_s=time.perf_counter()-started)
    return RidgePreparedReplay(**values)


class RidgeReplayProcessor(ContinuousProcessor):
    def __init__(self, *, variant, calibration, stripe_config, exposure_lines=480,
                 exposure_revision=0, selection_v4=None, batch_geometry=False):
        if variant not in POLICIES:
            raise ValueError('explicit supported ridge replay policy required')
        if not isinstance(batch_geometry, bool):
            raise ValueError('batch_geometry must be bool')
        self.variant = variant
        self.batch_geometry = batch_geometry or variant == 'ridge_primary_v4_batch'
        self.selection_v4 = selection_v4 or SelectionConfigV4(legacy=DEFAULT_SELECTION)
        self.last_selection_s = self.last_packet_decode_s = 0.
        super().__init__(calibration=calibration, stripe_config=stripe_config,
            selection=self.selection_v4.legacy, mode='static', exposure_lines=exposure_lines,
            exposure_revision=exposure_revision)

    def _decode_compact_capture(self, capture):
        started = time.perf_counter()
        diag = capture.diagnostics
        result = decode_slc2(capture.profile_payload, frame_width=diag.width, frame_height=diag.height,
                            request_id=capture.request_id, capture_epoch=capture.epoch)
        self.last_packet_decode_s = time.perf_counter()-started
        return result

    def _select_compact_capture(self, packet, gray):
        started = time.perf_counter()
        if self.variant in ('ridge_primary_v4', 'ridge_primary_v4_batch'):
            selected, primary = select_ridge_candidates(packet, gray, self.stripe_config, self.selection_v4)
            result = ((selected.center_q12, selected.accepted,
                       selected.retained & ~selected.accepted, selected.reasons), primary)
        else:
            selected = select_candidates_v4(gray, extract_candidates(gray, self.stripe_config),
                                            self.stripe_config, self.selection_v4)
            centers = np.asarray([row.candidate.center_q12 if row.candidate else 0 for row in selected.rows], np.int64)
            accepted = np.asarray([row.candidate is not None and row.reason is None for row in selected.rows])
            rejected = np.asarray([row.candidate is not None and row.reason is not None for row in selected.rows])
            result = ((centers, accepted, rejected, tuple(row.reason for row in selected.rows)),
                      np.zeros(gray.shape[0], bool))
        self.last_selection_s = time.perf_counter()-started
        return result

    def _reconstruct_section(self, *args):
        if self.batch_geometry:
            from sl_geometry.section_batch import reconstruct_section_batch
            return reconstruct_section_batch(*args)
        return super()._reconstruct_section(*args)
