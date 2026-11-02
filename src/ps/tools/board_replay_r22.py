"""Finite R22 ARM/HDMI replay preparation. Never loads FPGA/camera/motor.

Requires explicit execution on the actual ARM board and an already loaded,
hash-matching overlay. Saved simulated images never establish optical accuracy.
"""
from pathlib import Path
import argparse
import hashlib
import json
import platform
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
for directory in (ROOT,ROOT/'structured_light_system_algorithm_v1/src/python',ROOT/'structured_light_system_geometry_v1/src/python'):
    sys.path.insert(0,str(directory))


def valid_vdma_ack(receipt):
    return (isinstance(receipt,dict) and receipt.get('hdmi_acknowledged') is True
      and receipt.get('acknowledgement_basis')=='VDMA frame event and active park store; not optical proof'
      and type(receipt.get('active_store')) is int and 0<=receipt['active_store']<3
      and type(receipt.get('update_count')) is int and receipt['update_count']>0
      and receipt.get('fake_owner') is not True)


class ReplayOwner:
    def __init__(self,config):
        from virtual_scan.journal_owner_r20g import ram_journal_owner
        self.owner=ram_journal_owner(config)
    def configure(self):return self.owner.configure()
    def show_canvas(self,canvas):
        receipt=self.owner.show_canvas(canvas)
        if not valid_vdma_ack(receipt):raise RuntimeError('missing real HDMI/VDMA confirmation')
        return receipt
    def show_canvas_startup(self,canvas,timeout_s=20.):
        receipt=self.owner.show_canvas_startup(canvas,timeout_s=timeout_s)
        if not valid_vdma_ack(receipt):raise RuntimeError('missing real HDMI/VDMA confirmation')
        return receipt
    def render_and_show(self,renderer,gray,points,**options):
        receipt=self.owner.render_and_show(renderer,gray,points,options)
        if not valid_vdma_ack(receipt):raise RuntimeError('missing real HDMI/VDMA confirmation')
        return receipt
    def close(self,timeout_s=5.):
        receipt=self.owner.close(timeout_s=timeout_s)
        clean=(receipt.get('closed_confirmed') is True and receipt.get('owner_exited') is True
               and receipt.get('returncode')==0 and receipt.get('logs_persisted') is True)
        return dict(receipt,owner_closed=clean)


def save(path,value):
    import os
    with Path(path).open('x',encoding='utf8') as stream:
        json.dump(value,stream,indent=2,allow_nan=False);stream.flush();os.fsync(stream.fileno())


def writer_cleanup_confirmed(output,expected_runs):
    try:
        paths=list(Path(output).glob('*/CLEANUP.json'))
        if len(paths)!=expected_runs:return False
        receipts=[json.loads(path.read_text()) for path in paths]
        return all(isinstance(row,dict) and row.get('writer_worker_joined') is True
            and row.get('acquisition_worker_joined',True) is True
            and row.get('writer_durable') is True
            and row.get('writer_accepted')==row.get('writer_written')
            and row.get('unadmitted_source_save_error') is None
            and row.get('acquisition_error') is None and row.get('spool_error') is None
                   and row.get('writer_worker_unconfirmed') is False for row in receipts)
    except (OSError,ValueError,TypeError):return False


def quarantine_owner(output,*,sleeper=time.sleep):
    try:save(Path(output)/'OWNER_UNCONFIRMED.json',dict(retain_lock=True,external_recovery_required=True))
    except BaseException:pass  # Full/broken media must not bypass lock retention.
    while True:sleeper(.5)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--raster-manifest',type=Path)
    parser.add_argument('--input-root',type=Path,default=ROOT/'inputs',help='separately staged frozen T05/T06 native-image directories')
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--raw-codec',choices=('none','zlib1'),default='zlib1')
    parser.add_argument('--writer-group-size',type=int,default=8)
    parser.add_argument('--writer-max-delay-s',type=float,default=.05)
    args=parser.parse_args();wall_started=time.perf_counter()
    if platform.system()!='Linux' or platform.machine() not in ('armv7l','armv7'):
        raise RuntimeError('explicit ARM Linux board execution required; no connection is made')
    if not 1<=args.repeats<=3:raise ValueError('finite 1..3 repeats only')
    from tools.replay_r22 import replay_target
    from candidate.multibranch_fast import native_available
    from candidate.formal_resources_r22 import managed_data_bytes
    from candidate.runtime_monitor_r22 import RuntimeMonitor,linux_sample
    from candidate.display_r21 import renderer_factory
    from virtual_scan.display_r22 import SessionDisplay,SessionDisplayStartupError
    from structured_light.motion.lifecycle import HardwareProcessLock
    from resource_policy import capacity_plan,StorageBudget
    from acceptance_r22 import evaluate_trace
    import cv2
    from pynq import PL
    if not native_available():raise RuntimeError('native ARM multibranch library required')
    manifest=json.loads(args.manifest.read_text())
    current=Path(PL.bitfile_name)
    if hashlib.sha256(current.read_bytes()).hexdigest()!=manifest['files']['bit']['sha256']:
        raise RuntimeError('already loaded overlay does not match explicit HDMI manifest')
    if args.output.exists():raise FileExistsError(args.output)
    import shutil
    existing=managed_data_bytes(args.output.parent)
    free=lambda:shutil.disk_usage(args.output.parent).free
    plan=capacity_plan(args.repeats*480,4*1024*1024,existing_data_bytes=existing,filesystem_free_bytes=free())
    if not plan['admitted']:raise RuntimeError('capacity admission failed: '+str(plan))
    args.output.mkdir();budget=StorageBudget(existing,free);stop=threading.Event()
    lock=HardwareProcessLock(label='r22_finite_no_motion_replay').acquire()
    display=monitor=None;clean=False;results=[];started_runs=0
    try:
        save(args.output/'ADMISSION.json',dict(capacity=plan,loaded_bit_sha256=manifest['files']['bit']['sha256'],
            machine=platform.machine(),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            motor_commands=0,camera_commands=0,FPGA_loads=0,FPGA_stream_execution=False))
        monitor=RuntimeMonitor(args.output/'resources.jsonl',storage_budget=budget,scheduler_stop=stop)
        monitor.start()
        config=dict(frame_total=240,manifest=str(args.manifest),owner_log=str(args.output/'HDMI_OWNER.log'),
                    hardware_lock_fd=lock._handle.fileno(),display_source='ARM REPLAY / SAVED SIMULATED IMAGES / NOT METROLOGY')
        if args.raster_manifest:config['raster_manifest']=str(args.raster_manifest)
        cv2.setNumThreads(1)
        try:display=SessionDisplay(owner_factory=ReplayOwner,renderer_factory=renderer_factory,config=config,startup_timeout_s=65.)
        except SessionDisplayStartupError as exc:display=exc.display;raise
        for repeat in range(1,args.repeats+1):
            for target in ('T05','T06'):
                # Prior per-run ACKs have already been saved; prevent repeated
                # target names from including old timestamps in a later run.
                with display._condition:display.records.clear()
                before=linux_sample()
                started_runs+=1
                result=replay_target(target,args.raw_codec,args.output/f'{target}_{repeat:02d}',display,budget,None,240,stop,
                    input_root=args.input_root,writer_group_size=args.writer_group_size,writer_max_delay_s=args.writer_max_delay_s)
                after=linux_sample();ticks=after['cpu_total_ticks']-before['cpu_total_ticks']
                result.update(evidence_type='REAL_BOARD_REPLAY_COMPLETE_CHAIN',ARM_speed_verified=True,
                    display_owner='real',display_hardware_ACK=True,real_HDMI_verified=True,board_access=True,
                    CPU_total_capacity_pct=None if ticks<=0 else 100*(1-(after['cpu_idle_ticks']-before['cpu_idle_ticks'])/ticks),
                    mem_available_min_bytes=monitor.minimum_available,repeat=repeat,
                    data_correct=result['writer']['durable'] and result['writer']['written']==240,
                    bounded_queues=result['peak_pending_items']<=32 and result['peak_pending_bytes']<=64*1024*1024)
                result['performance_acceptance']=evaluate_trace(result)
                # A production-only window is diagnostic. Durable throughput
                # must also cover log synchronization, writer drain and export.
                full=result['performance_acceptance']['windows']['durable_times_s']['full']
                goals=result['performance_acceptance']['goals']
                goals['durable_full_interval']=bool(full['available'] and full['minimum_hz']>=10.)
                result['performance_acceptance']['observed_goals_passed']=all(goals.values())
                result['board_replay_performance_verified']=result['performance_acceptance']['observed_goals_passed']
                save(args.output/f'{target}_{repeat:02d}'/'BOARD_RESULT.json',result);results.append(result)
        close=display.close(timeout_s=30.);resource=monitor.close();clean=close['owner_closed'] and close['worker_joined']
        passed=(clean and resource['CPU_goal_passed'] and resource['memory_goal_passed'] and not resource['error']
                and not resource['stop_reason'] and all(r['board_replay_performance_verified'] for r in results))
        save(args.output/'DONE.json',dict(passed=passed,runs=results,display=close,resource=resource,
            total_wall_s=time.perf_counter()-wall_started,FPGA_stream_execution=False,physical_accuracy_verified=False,
            motor_commands=0,camera_commands=0,FPGA_loads=0))
        return 0 if passed else 1
    except BaseException as exc:
        save(args.output/'ERROR.json',dict(passed=False,error=type(exc).__name__+': '+str(exc),
             partial_evidence_preserved=True,motor_commands=0,camera_commands=0,FPGA_loads=0))
        raise
    finally:
        monitor_joined=True
        if monitor is not None:
            try:monitor_joined=monitor.close()['worker_joined'] is True
            except BaseException:monitor_joined=False
        if display is not None and not clean:
            try:
                receipt=display.close(timeout_s=30.);clean=receipt['owner_closed'] and receipt['worker_joined']
            except BaseException:clean=False
        elif display is None:clean=True
        clean=clean and monitor_joined and writer_cleanup_confirmed(args.output,started_runs)
        if clean:lock.release()
        else:
            # Preserve exclusive ownership if HDMI/CMA cannot be confirmed
            # stopped. There is no automatic kill, reset or lock release.
            quarantine_owner(args.output)


if __name__=='__main__':raise SystemExit(main())
