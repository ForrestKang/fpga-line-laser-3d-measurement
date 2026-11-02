"""SLC1/PSF1 immutable vector admission; NLQ1 retains observation semantics."""
from dataclasses import dataclass
import numpy as np

from ..p2.errors import ProtocolError
from .stream_features import FeatureConfiguration
from .continuous_quality import FrameQuality, QualityThresholds, assess_observed_quality

SLC1_MAGIC = 0x534c4331
SLC1_ABI = 0x10000
SLC1_MAX_BYTES = 4 * (32 + 3 * 480)
NLQ1 = 0x4e4c5131
PSF1 = 0x50534631
STATUSES = ('good', 'saturated', 'ambiguous', 'weak', 'incomplete', 'unvalidated')
COMPACT_DMA_CONTRACT = dict(enabled=True, format='SLC1', abi=SLC1_ABI,
    capability_magic=0x52435131, capability_offset=0x140, abi_offset=0x144,
    mode_offset=0x13c, mode_value=2, max_packet_bytes=SLC1_MAX_BYTES,
    quality_policy=NLQ1, prefilter_policy=PSF1)


def _median2(values):
    return int(np.median(values) * 2) if len(values) else 0


@dataclass(frozen=True)
class CompactPacket:
    payload: bytes
    request_id: int
    epoch: int
    configuration: FeatureConfiguration

    @property
    def header(self):
        return np.frombuffer(self.payload, '<u4', count=32)

    @property
    def vectors(self):
        return np.frombuffer(self.payload, '<u4', offset=128).reshape(-1, 3)

    @property
    def columns(self):
        a, b, c = self.vectors.T
        return (a & 0x3fffff, (a >> 22) & 31, (a & (1 << 27)) != 0,
                (a & (1 << 28)) != 0, (a & (1 << 29)) != 0,
                b & 1023, (b >> 10) & 1023, (b >> 20) & 1023,
                c & 1023, (c >> 10) & 255, (c >> 18) & 255,
                (c & (1 << 26)) != 0)


def decode_slc1(payload, *, frame_width, frame_height, request_id=None, capture_epoch=None):
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ProtocolError('SLC1 payload must be bytes-like')
    data = bytes(payload)
    if len(data) < 140 or len(data) > SLC1_MAX_BYTES or len(data) % 4:
        raise ProtocolError('SLC1 packet length mismatch')
    h = np.frombuffer(data, '<u4', count=32).astype(np.int64)
    if (h[0] != SLC1_MAGIC or h[1] != SLC1_ABI or h[2] * 4 != len(data)
            or not 1 <= h[10] <= 480 or h[2] != 32 + 3 * h[10]):
        raise ProtocolError('SLC1 magic/ABI/word count mismatch')
    if (type(frame_width) is not int or type(frame_height) is not int
            or not 1 <= frame_width <= 752 or not 1 <= frame_height <= 480
            or tuple(h[11:13]) != (frame_width, frame_height)):
        raise ProtocolError('SLC1 frame geometry mismatch')
    if ((request_id is not None and h[3] != request_id)
            or (capture_epoch is not None and h[4] != capture_epoch)):
        raise ProtocolError('SLC1 capture identity/epoch mismatch')
    if (h[4] >> 16 or h[9] >> 16 or h[13] >> 8 or h[14] >> 20 or h[15] >> 20
            or h[16] >> 20 or h[17] >> 18 or h[18] != ((255 << 8) | (h[18] & 255))
            or h[19] != NLQ1 or h[30] != PSF1 or h[31]):
        raise ProtocolError('SLC1 reserved bits/policy mismatch')
    x0, x1 = int(h[14] & 1023), int(h[14] >> 10)
    y0, y1 = int(h[15] & 1023), int(h[15] >> 10)
    lo, hi = int(h[16] & 1023), int(h[16] >> 10)
    if (not 0 <= x0 < x1 <= frame_width or not 0 <= y0 < y1 <= frame_height
            or h[10] != y1-y0 or h[20] != h[10] or not 1 <= lo <= hi <= 1023):
        raise ProtocolError('SLC1 configuration bounds mismatch')
    cfg = FeatureConfiguration(frame_width, frame_height, int(h[13]), (x0,y0,x1,y1),
                               lo,hi,int(h[17]),int(h[18]&255),255)
    packet = CompactPacket(data,int(h[3]),int(h[4]),cfg)
    w = packet.vectors
    if np.any(w[:,0] >> 30) or np.any(w[:,1] >> 30) or np.any(w[:,2] >> 27):
        raise ProtocolError('SLC1 reserved row bits')
    center,flags,valid,fallback,fault,first,last,count,width,rawpeak,peak,sat = packet.columns
    single = (count == 1) & ~fault
    positive = count > 0
    if (np.any(fallback != ((count > 1) | fault))
            or np.any((~single) & ((center != 0) | (flags != 0)))
            or np.any(~positive & ((first != 0)|(last != 0)|(width != 0)|(peak != 0)|sat|valid|(rawpeak > h[13])))
            or np.any(positive & ((first < x0)|(last >= x1)|(first > last)
                |(width != last-first+1)|(count > (width+1)//2)|(peak == 0)
                |(rawpeak <= h[13])|(peak > 255-h[13])|(peak != rawpeak-h[13])))
            or np.any(positive & (sat != (rawpeak == 255)))
            or np.any(single & ((center < (first.astype(np.int64)<<12))
                |(center > (last.astype(np.int64)<<12)) ))):
        raise ProtocolError('SLC1 support/fallback/center mismatch')
    expected = (((width < lo)|(width > hi)).astype(np.uint32)
                | (sat.astype(np.uint32) << 2)
                | (((first == x0)|(last == x1-1)).astype(np.uint32) << 3))
    if (np.any(single & ((flags & 13) != expected))
            or np.any(single & (peak < max(1,int(h[18]&255))) & ((flags & 2) == 0))
            or np.any(valid != (single & ((flags & 19) == 0)))):
        raise ProtocolError('SLC1 preclassification mismatch')
    n = y1-y0
    if (h[8] != np.count_nonzero(valid) or h[23] != np.count_nonzero(count > 1)
            or h[22] != np.count_nonzero(rawpeak >= 250)
            or not h[22] <= h[21] <= h[22]*(x1-x0)
            or h[24] != _median2(rawpeak) or h[25] != _median2(peak[valid])
            or h[26] != _median2(width[valid])
            or h[27] != (int(width[valid].max()) if valid.any() else 0)
            or h[29] != np.count_nonzero(single & ~fallback & (flags == 0))):
        raise ProtocolError('SLC1 quality summary/vector mismatch')
    status = (1 if h[21]*100 > n*(x1-x0) or h[22]*20 > n else
              2 if h[23]*20 > n or h[27] > 32 else
              3 if h[8] == 0 and h[24] < 120 else
              4 if h[8]*5 < n*4 else 3 if h[25] < 120 else 0)
    if h[28] != status:
        raise ProtocolError('SLC1 NLQ1 status mismatch')
    return packet


def compact_quality(packet):
    h = packet.header
    n, valid = int(h[20]), int(h[8])
    x0, _, x1, _ = packet.configuration.roi
    return FrameQuality(STATUSES[int(h[28])], valid/n,
        float(h[25])/2 if valid else None, float(h[26])/2 if valid else None,
        int(h[21])/(n*(x1-x0)), int(h[23])/n, valid,n,{},True,int(h[22]))


def compact_geometry_quality(packet, accepted, *, rejection_counts=None,
                             thresholds=QualityThresholds()):
    """Final v3 mask/raw peaks; NLQ1 status is never geometry admission."""
    _,_,_,_,_,_,_,count,width,rawpeak,_,_ = packet.columns
    h = packet.header
    x0,_,x1,_ = packet.configuration.roi
    return assess_observed_quality(row_valid=np.asarray(accepted),widths_px=width,intensities=rawpeak,
        candidate_counts=count,raw_row_peaks=rawpeak,saturation_pixels=int(h[21]),saturated_rows=int(h[22]),
        roi_width=x1-x0,rejection_counts=rejection_counts,thresholds=thresholds)


def materialize_compact_qa(frame, diagnostics, extension):
    """Explicit QA only: compare new words against independent Raw sums."""
    from dataclasses import replace
    from ..profile import Profile, ProfilePoint
    packet = frame.compact_packet
    cfg = packet.configuration
    gray = np.frombuffer(frame.raw,np.uint8).reshape(cfg.height,cfg.width)
    x0,y0,x1,y1 = cfg.roi
    expected = []
    columns = packet.columns
    for y in range(cfg.height):
        roi = gray[y,x0:x1] if y0 <= y < y1 else np.zeros(0,np.uint8)
        support = [x0+i for i,v in enumerate(roi) if int(v) > cfg.fixed_threshold]
        s0 = sum(int(gray[y,x])-cfg.fixed_threshold for x in support)
        s1 = sum(x*(int(gray[y,x])-cfg.fixed_threshold) for x in support)
        center = (s1 << 12)//s0 if s0 else 0
        first,last = (support[0],support[-1]) if support else (0,0)
        sat255 = sum(int(gray[y,x]) == 255 for x in support)
        rawpeak = int(roi.max()) if len(roi) else 0
        sat250 = int(np.count_nonzero(roi >= 250))
        expected.append((first|(last<<10)|(sat255<<20),center|(rawpeak<<22),sat250))
        if y0 <= y < y1:
            row = diagnostics.rows[y]
            i = y-y0
            c,flags,valid,fallback,fault,f,l,count,width,rp,peak,sat = (v[i] for v in columns)
            components = sum(i == 0 or x != support[i-1]+1 for i,x in enumerate(support))
            span = last-first+1 if support else 0
            contrast = max((int(gray[y,x])-cfg.fixed_threshold for x in support),default=0)
            transport = bool(row.flags & 0x30)
            classification = 0
            if components == 1 and not transport:
                classification = (int(span < cfg.min_width or span > cfg.max_width)
                    | (int(s0 < max(1,cfg.min_sum) or contrast < max(1,cfg.min_peak))<<1)
                    | (int(sat255>0)<<2) | (int(first==x0 or last==x1-1)<<3)
                    | (int(s0>(1<<18)-1 or s1>(1<<27)-1 or center>(1<<22)-1)<<4))
            if ((int(c),int(flags),bool(valid),bool(fallback),bool(fault),int(f),int(l),int(count),int(width),int(rp),int(peak),bool(sat))
                    != (center if components == 1 and not transport else 0,classification,row.valid,
                        components>1 or transport,transport,first,last,components,span,rawpeak,contrast,sat255>0)):
                raise ProtocolError('SLC1 same-frame Raw/compact row QA mismatch')
    if not np.array_equal(np.asarray(extension,dtype=np.uint32),np.asarray(expected,dtype=np.uint32)):
        raise ProtocolError('SLC1 same-frame extended diagnostic/Raw QA mismatch')
    h = packet.header
    if int(h[21]) != int(np.count_nonzero(gray[y0:y1,x0:x1] >= 250)):
        raise ProtocolError('SLC1 Raw saturation pixel QA mismatch')
    points = tuple(ProfilePoint(row=r.row,xc_fixed=r.xc_fixed,quality=r.quality,valid=r.valid,flags=r.flags)
                   for r in diagnostics.rows)
    profile = Profile(format_version=2,profile_id=frame.request_id,angle_raw=int(h[5]),
        timestamp=int(h[6])|(int(h[7])<<32),error_flags=int(h[9]),points=points,
        frame_width=cfg.width,frame_height=cfg.height)
    return replace(frame,profile=profile,diagnostics=diagnostics)
