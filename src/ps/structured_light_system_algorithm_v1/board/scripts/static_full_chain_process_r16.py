"""Opt-in spawned CPU selection/geometry; static owner retains all hardware."""
import static_full_chain_r15 as original
import static_segments_bench_process_r16 as process_bench

_baseline_dependencies=original.make_dependencies


def make_dependencies(args):
    deps=_baseline_dependencies(args)
    from structured_light.scan3d.cpu_stage_process_r16 import CPUStageProcess
    stage=CPUStageProcess(args.camera,args.plane,timeout_s=5.)
    deps.prepare_processing=stage.start
    deps.select=stage.select
    if args.camera is not None:
        deps.process_selected=stage.geometry
        deps.geometry_declaration=dict(deps.geometry_declaration,
            software_candidate='r16_spawned_cpu_local_fallback_cached_context',
            original_range_and_training_bounds_enforced=True)
    original_close=deps.close
    def close(state,**kwargs):
        try:
            receipt=stage.close()
            state['cpu_stage_process']=receipt
            if receipt.get('closed') is not True:
                raise RuntimeError('CPU-only worker close unconfirmed')
        except BaseException as exc:
            deps.base._record_error(state,'cpu_stage_close',exc)
        finally:
            original_close(state,**kwargs)
    deps.close=close
    return deps


def main(argv=None):
    original.bench=process_bench
    original.make_dependencies=make_dependencies
    return original.main(argv)


if __name__=='__main__':main()
