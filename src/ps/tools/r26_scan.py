"""Single reproducible entrypoint for the R26 full chain (board, no motion).

Runs: saved Raw -> actual PL -> measurement and preview geometry -> cloud ->
HDMI snapshots -> complete persistence -> preview XYZ persistence -> post-scan
feature/dimension export.

It only fills in the verified defaults; every argument it does not override is
still handled by pl_replay_r25.py, so validation, capacity admission and
persistence semantics are the proven ones.
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / 'structured_light_system_algorithm_v1/src/python',
          ROOT / 'structured_light_system_geometry_v1/src/python'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',choices=('actual_pl','offline','camera'),default='actual_pl')
    parser.add_argument('--saved-run-root',type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--data-root', type=Path, default=Path('data'))
    parser.add_argument('--input-root', type=Path, default=Path('inputs'))
    parser.add_argument('--manifest', type=Path, default=Path('runtime/r25_v9_prep/manifest.json'))
    parser.add_argument('--limit', type=int, default=240)
    parser.add_argument('--targets', nargs='+', default=['T05', 'T06'], choices=('T05', 'T06'))
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--display-backend', default='process', choices=('thread', 'process'))
    # 0 preserves the historical every-delivery render.  A positive value is a
    # bounded latest-image cadence; the display may coalesce old pending frames,
    # while every measurement frame and every save remains intact.
    parser.add_argument('--display-min-interval-ms', type=float, default=150.0)
    parser.add_argument('--display-native-finite-validation-only', action='store_true',
                        help='R28 A/B: native renderer validates finiteness in its fused pass')
    parser.add_argument('--display-release-storage-on-close', action='store_true',
                        help='release shared display references only after confirmed owner exit')
    parser.add_argument('--display-sample-cap', type=int, default=16000,
                        help='bounded model-panel sample cap (process display); '
                             'all XYZ still reaches the display cloud and disk')
    parser.add_argument('--geometry-backend', default='prepared_serial',
                        choices=('separate', 'paired', 'prepared_serial'))
    parser.add_argument('--raw-codec', default='zlib1', choices=('none', 'zlib1'))
    parser.add_argument('--payload-preallocate-mib',type=int,choices=range(0,65),default=0)
    parser.add_argument('--storage-backend', choices=('chunk_v3','journal15c'), default='journal15c',
                        help='journal15c: release single-file data/index commit; complete sync still required; chunk_v3 retained for explicit comparison')
    parser.add_argument('--journal-buffer-kib',type=int,choices=(0,512,2048),default=512)
    parser.add_argument('--writer-group-size', type=int, default=8)
    # Default 50 ms preserves the proven policy; 250 ms is an explicit A/B
    # experiment that still fsyncs every group and the final drain.
    parser.add_argument('--writer-max-delay-ms', type=float, default=50.0)
    parser.add_argument('--writer-validation-phase', choices=('submit','worker'), default='worker')
    # A release requires a built and runtime-bound native validator. Strict
    # Python is permitted only by the explicit diagnostic option below.
    parser.add_argument('--pfo3-validator-manifest', type=Path, default=None)
    parser.add_argument('--raster-manifest', type=Path,
                        default=Path('runtime/assets/native_arm_r13/point_raster_manifest.json'))
    parser.add_argument('--feature-policy', choices=('v4','v5','v6'), default='v6')
    parser.add_argument('--feature-workers', type=int, choices=(1, 2), default=1,
                        help='bounded post-scan slice workers; only used with the full chain')
    full_chain = parser.add_mutually_exclusive_group()
    full_chain.add_argument('--no-full-chain', action='store_true',
                            help='disable preview persistence and post-scan features (diagnostic only)')
    full_chain.add_argument('--full-chain', dest='no_full_chain', action='store_false',
                            help='explicitly retain the default complete pipeline')
    parser.set_defaults(no_full_chain=False)
    parser.add_argument('--diagnostic-python', action='store_true',
                        help='explicit non-performance mode permitting strict Python backends')
    parser.add_argument('--preflight-only', action='store_true',
                        help='validate release dependencies without hardware or output writes')
    args = parser.parse_args(argv)

    if args.source=='camera':
        raise RuntimeError('camera source contract ready; live optical acceptance pending, use explicit admitted continuous_scan workflow')
    if args.source=='offline':
        if args.saved_run_root is None or len(args.targets)!=1:
            raise ValueError('offline requires --saved-run-root and exactly one --targets value')
        if args.no_full_chain:raise ValueError('offline release requires complete processing')
        if args.preflight_only:raise ValueError('offline preflight uses strict saved-run admission at start')
        if args.repeats != 1:raise ValueError('offline currently supports one saved-run pass; use separate new output IDs')
        # Never silently accept an option which this saved-packet adapter does
        # not implement. It draws PNGs, not physical HDMI, and uses the saved
        # source configuration rather than live DMA/native-manifest options.
        unsupported={'--display-native-finite-validation-only','--display-release-storage-on-close','--journal-buffer-kib','--storage-backend','--payload-preallocate-mib','--data-root','--input-root','--manifest','--display-backend',
            '--display-min-interval-ms','--display-sample-cap','--geometry-backend',
            '--raw-codec','--writer-group-size','--writer-max-delay-ms',
            '--writer-validation-phase','--pfo3-validator-manifest','--raster-manifest',
            '--diagnostic-python'}
        supplied={value.split('=',1)[0] for value in argv if value.startswith('--')}
        rejected=sorted(supplied & unsupported)
        if rejected:raise ValueError('unsupported offline override: '+', '.join(rejected))
        from tools.offline_scan_r26 import main as offline_main
        return offline_main(['--run-root',str(args.saved_run_root),'--target',args.targets[0],
                             '--output',str(args.output),'--limit',str(args.limit),
                             '--feature-policy',args.feature_policy,
                             '--feature-workers',str(args.feature_workers)])
    from tools.pl_replay_r25 import main as replay_main
    validator_manifest = args.pfo3_validator_manifest
    if validator_manifest is None:
        built = ROOT / 'candidate/native/pfo3_validate_manifest_r25.json'
        if built.is_file():
            validator_manifest = built
    forwarded = ['--manifest', str(args.manifest), '--input-root', str(args.input_root),
                 '--output', str(args.output), '--data-root', str(args.data_root),
                 '--limit', str(args.limit), '--repeats', str(args.repeats),
                 '--targets', *args.targets,
                 '--display-backend', args.display_backend,
                 '--display-min-interval-ms', str(args.display_min_interval_ms),
                 '--geometry-backend', args.geometry_backend,
                 '--raw-codec', args.raw_codec,
                 '--payload-preallocate-mib', str(args.payload_preallocate_mib),
                 '--storage-backend', args.storage_backend,
                 '--journal-buffer-kib', str(args.journal_buffer_kib),
                 '--writer-group-size', str(args.writer_group_size),
                 '--writer-max-delay-ms', str(args.writer_max_delay_ms),
                 '--writer-validation-phase', args.writer_validation_phase,
                 '--feature-workers', str(args.feature_workers),
                 '--feature-policy', args.feature_policy,
                 '--raster-manifest', str(args.raster_manifest)]
    if args.display_sample_cap is not None:
        forwarded.extend(['--display-sample-cap', str(args.display_sample_cap)])
    if args.display_native_finite_validation_only:
        forwarded.append('--display-native-finite-validation-only')
    if args.display_release_storage_on_close:
        forwarded.append('--display-release-storage-on-close')
    if validator_manifest is not None:
        forwarded.extend(['--pfo3-validator-manifest', str(validator_manifest)])
    from candidate.release_contract_r26 import validate_release, resolved_config
    dependencies = validate_release(validator_manifest, diagnostic=args.diagnostic_python)
    if args.preflight_only:
        import json
        print(json.dumps(dict(config=resolved_config(args), dependencies=dependencies), sort_keys=True))
        return 0
    forwarded.extend(['--release-config-json', __import__('json').dumps(
        dict(config=resolved_config(args), dependencies=dependencies), sort_keys=True)])
    if not args.no_full_chain:
        forwarded.append('--full-chain')
    return replay_main(forwarded)


if __name__ == '__main__':
    raise SystemExit(main())
