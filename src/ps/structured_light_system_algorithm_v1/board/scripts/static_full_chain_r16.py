"""R16 static candidate: admitted R15 bit, local fallback and cached geometry.

No calibration gates, motion authority or original capture lifecycle changes.
"""
import static_full_chain_r15 as original

_baseline_dependencies=original.make_dependencies


def make_dependencies(args):
    deps=_baseline_dependencies(args)
    from structured_light.scan3d.local_segments_r16 import select_local_segment_candidates
    deps.select=select_local_segment_candidates
    if args.camera is not None:
        from structured_light.scan3d.selected_geometry_r14 import SelectedGeometryBenchmark
        deps.process_selected=SelectedGeometryBenchmark(args.camera,args.plane,
            vector_geometry=args.vector_geometry,cached_context=True)
        deps.geometry_declaration=dict(deps.geometry_declaration,
            software_candidate='r16_local_fallback_prevalidated_geometry',
            original_range_and_training_bounds_enforced=True)
    return deps


def main(argv=None):
    # Explicit dependency injection into the retained tested static lifecycle.
    original.make_dependencies=make_dependencies
    return original.main(argv)


if __name__=='__main__':main()
