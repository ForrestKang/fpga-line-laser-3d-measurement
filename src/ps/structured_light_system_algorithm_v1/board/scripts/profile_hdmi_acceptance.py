"""Bounded, operator-observed acceptance in one Profile/HDMI Overlay session."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import time
import uuid


def write_json(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    temporary.replace(path)


def wait_confirmation(output,phase,nonce,timeout_s):
    path=output/(phase+'_confirmation.json')
    deadline=time.monotonic()+timeout_s
    while time.monotonic()<deadline:
        if path.is_file():
            result=json.loads(path.read_text(encoding='utf-8'))
            if result.get('nonce')!=nonce or result.get('phase')!=phase:
                raise ValueError('operator confirmation belongs to a different session or phase')
            if result.get('approved') is not True or not result.get('user_statement'):
                raise RuntimeError('operator did not confirm '+phase)
            return result
        time.sleep(.2)
    raise TimeoutError('operator confirmation deadline: '+phase)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--operator-timeout-s',type=float,default=300)
    args=parser.parse_args()
    if not 1<=args.operator_timeout_s<=600: parser.error('operator timeout must be 1..600 s')
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    nonce=uuid.uuid4().hex
    record={'nonce':nonce,'stage':'profile_hdmi','hardware':'real_pynq_z2',
            'same_frame_profile_verified':False,'hdmi_display_verified':False,
            'profile_hdmi_coexistence_verified':False,'real_laser_profile_verified':False,
            'no_hidden_retry':True,'captures':[],'started_board_utc':datetime.now(timezone.utc).isoformat()}
    dashboard=None
    def status(phase):
        record['phase']=phase
        record['updated_board_utc']=datetime.now(timezone.utc).isoformat()
        write_json(output/'live_status.json',record)
        print(json.dumps({'phase':phase,'nonce':nonce,'captures':len(record['captures'])}),flush=True)
    try:
        status('initializing')
        from profile_dashboard_board import open_profile_dashboard
        from structured_light.p2.hdmi_output import colorbar_bgr
        dashboard=open_profile_dashboard(args.config,output,hdmi=True,background_owner=True)
        record['session']=dashboard.board_context
        record['test_pattern_snapshot']=dashboard.display.show(colorbar_bgr())
        status('test_pattern')
        record['test_pattern_confirmation']=wait_confirmation(output,'test_pattern',nonce,args.operator_timeout_s)
        for index in range(10):
            if index==5:
                status('scene_change')
                record['scene_change_confirmation']=wait_confirmation(output,'scene_change',nonce,args.operator_timeout_s)
            status('capture_'+str(index))
            result=dashboard.capture_and_compare()
            capture=result['result']
            item={'index':index,'request_id':capture.request_id,'epoch':capture.epoch,
                  'session_id':capture.session_id,'evidence':str(result['directory'].relative_to(output)),
                  'metrics':result['metrics'],'hdmi_update':result['hdmi_update']}
            if record['captures']:
                previous=record['captures'][-1]
                if item['epoch']!=previous['epoch'] or item['session_id']!=previous['session_id'] or item['request_id']!=previous['request_id']+1:
                    raise RuntimeError('request/epoch/session association changed during coexistence test')
            record['captures'].append(item)
            dashboard.save_result()
            write_json(output/'capture_records.json',record['captures'])
        record['same_frame_profile_verified']=all(x['metrics']['comparison_passed'] for x in record['captures'])
        status('final_visual')
        record['final_visual_confirmation']=wait_confirmation(output,'final_visual',nonce,args.operator_timeout_s)
        record['hdmi_display_verified']=True
        record['profile_hdmi_coexistence_verified']=record['same_frame_profile_verified'] and len(record['captures'])==10
        record['phase']='verified'
        return_code=0
    except Exception as error:
        record.update(phase='failed',error_type=type(error).__name__,error=str(error))
        return_code=1
    finally:
        if dashboard is not None:
            try:
                dashboard.close()
                record['display_stop_confirmed']=True
            except Exception as error:
                record['display_stop_confirmed']=False
                record['cleanup_error']=str(error)
                record['profile_hdmi_coexistence_verified']=False
                return_code=1
        record['finished_board_utc']=datetime.now(timezone.utc).isoformat()
        write_json(output/'summary.json',record)
        write_json(output/'live_status.json',record)
    return return_code


if __name__=='__main__': raise SystemExit(main())
