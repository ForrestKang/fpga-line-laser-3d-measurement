"""Single strict pass over encoded MBR1 views; no trust or ownership shortcut."""

def build_encoded_views_once(payload, header, length):
    """Build views with the strict reader instead of building and checking twice.

    ``payload`` is the newly copied immutable MBR1 bytes object. The existing
    strict reader validates every descriptor and constructs its own views of
    those exact bytes. No source array or previously validated view is reused.
    Keep ``check_encoded_once`` for callers supplying views, whose owner and
    address must still be checked independently.
    """
    from .storage_r21 import _header_schema, _profile_from_header, _check_profile, _ARRAYS
    if not isinstance(payload, bytes):
        raise ValueError('encoded MBR1 payload must be immutable bytes')
    _header_schema(header)
    result = _profile_from_header(payload, header, length, expand_reasons=False)
    _check_profile(result)
    views = {key: result[key] for key in _ARRAYS}
    views.update({'observation_quality/' + key: value
                  for key, value in result['observation_quality'].items()})
    views['observation_reasons_ids'] = result['observation_reasons']
    views['reasons_ids'] = result['reasons']
    return views


def check_encoded_once(payload, header, length, views):
    # Delayed import keeps the encoder's public API and strict decoder intact.
    from .storage_r21 import _header_schema, _profile_from_header, _check_profile
    descriptors, _ = _header_schema(header)
    if set(views) != set(descriptors):
        raise ValueError('sealed MBR1 array set mismatch')
    # This existing strict loop checks safe dtype, shape, byte count, ordered
    # offsets, contiguous immutable owner/address and complete body coverage.
    # Its reason-ID and profile checks are also required; none are removed.
    result = _profile_from_header(payload, header, length, expand_reasons=False, views=views)
    _check_profile(result)
