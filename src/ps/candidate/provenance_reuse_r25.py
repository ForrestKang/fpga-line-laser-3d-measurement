"""Opt-in digest reuse for one already validated actual-transport pair.

This module is used after actual transport completion. It does not decode,
hash, inspect Raw pixels, or issue a hardware completion. The transport must
first independently hash and validate its immutable primary/final pair when
constructing PLFrameAdmission, or reuse the assembler's exact immutable pair
seal. Disk readers and writer output hashing remain
independent. There is no global cache and no frame-ID/hash based cache lookup.
"""
from dataclasses import dataclass
from .pfo3_processor_r25 import PLFrameAdmission
from .pfo3_r25 import FinalFrame,PFO3Error


@dataclass(frozen=True)
class PacketDigestsR25:
    primary_sha256: str
    final_sha256: str


_PROOF_TOKEN=object()


@dataclass(frozen=True,init=False)
class CompletedPairProofR25:
    """Strong references to one completed immutable pair, not a DMA receipt.

    Create once immediately after actual transport admission. A later check
    still binds the admission, both bytes objects, exact frame, input SHA and
    all capture identity fields. Equal-content foreign objects cannot reuse
    the proof. Caller policy/status/geometry checks remain required.
    """
    _token: object
    _admission: object
    _frame: object
    _primary: bytes
    _payload: bytes
    _binding: tuple
    digests: PacketDigestsR25

    @staticmethod
    def _admission_binding(admission):
        return (admission.identity,admission.parameter_fingerprint,
                admission.bit_sha256,admission.stream_sha256,
                admission.hardware_completed,admission.fixture_only,
                admission.input_raw_sha256,admission._transport_capability,
                admission._validated_frame,admission._validated_primary,
                admission._primary_sha256)

    @classmethod
    def from_completion(cls,admission,frame,primary,*,input_raw_sha256,expected_identity):
        if cls is not CompletedPairProofR25 or type(admission) is not PLFrameAdmission:
            raise PFO3Error('exact actual completion admission required')
        admission.validate()
        if admission.fixture_only or admission.hardware_completed is not True:
            raise PFO3Error('digest reuse requires actual completion')
        if (type(frame) is not FinalFrame or type(primary) is not bytes
                or type(frame.payload) is not bytes
                or frame is not admission._validated_frame
                or primary is not admission._validated_primary
                or input_raw_sha256!=admission.input_raw_sha256
                or dict(expected_identity)!=dict(admission.identity)):
            raise PFO3Error('digest reuse requires exact completed immutable pair and identity')
        # __post_init__ hashed these objects or checked the assembler's exact
        # immutable content seal; neither mechanism issues hardware authority.
        # validate() above verifies the private actual-transport capability.
        proof=object.__new__(cls)
        for name,value in (('_token',_PROOF_TOKEN),('_admission',admission),
                ('_frame',frame),('_primary',primary),('_payload',frame.payload),
                ('_binding',cls._admission_binding(admission)),
                ('digests',PacketDigestsR25(admission._primary_sha256,admission.stream_sha256))):
            object.__setattr__(proof,name,value)
        return proof

    def check(self,admission,frame,primary,*,input_raw_sha256,expected_identity):
        if (type(self) is not CompletedPairProofR25 or getattr(self,'_token',None) is not _PROOF_TOKEN
                or admission is not self._admission or frame is not self._frame
                or primary is not self._primary or type(primary) is not bytes
                or frame.payload is not self._payload):
            raise PFO3Error('foreign completion/pair cannot reuse digest proof')
        # Compare immutable scalar values and object identities explicitly;
        # ndarray equality is neither a provenance check nor a scalar predicate.
        current=self._admission_binding(admission)
        if (any(current[i] is not self._binding[i] for i in (0,4,5,7,8,9))
                or any(current[i]!=self._binding[i] for i in (1,2,3,6,10))
                or input_raw_sha256!=current[6]
                or dict(expected_identity)!=dict(current[0])):
            raise PFO3Error('completed digest proof identity changed')
        return self.digests

    def check_receipt(self,receipt,admission,frame,primary,*,input_raw_sha256,expected_identity):
        digests=self.check(admission,frame,primary,input_raw_sha256=input_raw_sha256,
                           expected_identity=expected_identity)
        if (receipt.get('actual_PL_execution') is not True
                or receipt.get('primary_stream_sha256')!=digests.primary_sha256
                or receipt.get('assembled_sha256')!=digests.final_sha256):
            raise PFO3Error('transport receipt differs from completed pair')
        return digests
