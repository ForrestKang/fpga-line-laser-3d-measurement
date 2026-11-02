"""Describe failed-run spool tails without deleting evidence or claiming commits."""


def spool_authority(*, spool_points, accepted_points, accepted_frames, receipt, completed):
    values=(spool_points,accepted_points,accepted_frames)
    if any(type(value) is not int or value<0 for value in values):
        raise ValueError('spool authority requires nonnegative integer counts')
    if accepted_points>spool_points:
        raise ValueError('accepted points exceed measured spool')
    all_durable=bool(receipt is not None and getattr(receipt,'durable',False)
        and getattr(receipt,'accepted',None)==accepted_frames
        and getattr(receipt,'written',None)==accepted_frames)
    return dict(schema='scan3d_spool_authority15c_v1',
        spool_points=spool_points,accepted_points=accepted_points,
        accepted_frames=accepted_frames,unadmitted_tail_points=spool_points-accepted_points,
        all_accepted_frames_durable=all_durable,
        durable_points=accepted_points if all_durable else None,
        whole_spool_is_completed_cloud=bool(completed and all_durable and accepted_points==spool_points),
        recovery_source='strict committed frame records; raw spool is not commit authority',
        original_run_completed=bool(completed))
