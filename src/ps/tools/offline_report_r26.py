"""Human-readable report for one offline full-flow run (reads RESULT/VERIFY)."""
from __future__ import annotations
import argparse, json
from pathlib import Path


def fmt_feature(f):
    t = f.get('type')
    v = f.get('value_mm')
    if v is None:
        return "- %s: 不可测（%s） region=%s" % (t, f.get('reason'), f.get('region_id'))
    unit = f.get('unit', 'mm')
    support = len(f.get('support', {}).get('point_indices', []))
    return ("- %s = %.3f %s | region=%s | 支持点=%d | 残差=%s | 方向=%s"
            % (t, v, unit, f.get('region_id'), support, f.get('residual_mm'), f.get('direction')))


def build(run):
    run = Path(run)
    r = json.loads((run / 'RESULT.json').read_text())
    v = json.loads((run / 'VERIFY.json').read_text()) if (run / 'VERIFY.json').exists() else None
    feats = json.loads((run / 'features.json').read_text())['features']
    src = r['source']
    lines = ['# 离线全流程报告：' + r['target'], '']
    lines += ['## 1. 来源与证据类别', '',
              '- 保存包根：' + src['run_root'],
              '- bit SHA-256：' + src['bit_sha256'],
              '- 几何：' + src['calibration_id'] + '（' + src['geometry_evidence_type'] + '）',
              '- 标定文件 SHA-256：' + src['calibration_sha256'],
              '- 包来源：' + src['packet_origin'] + '；本进程执行 PL：' + str(src['PL_execution_this_process']),
              '- OpenCV 来源：' + str(r.get('cv2_source')), '']
    lines += ['## 2. 处理结果', '',
              '- 帧数：%d（有效 measurement 帧 %d，空帧 %d）' % (r['frames'], r['measurement']['valid_frames'],
                                                        r['measurement']['empty_frames']),
              '- measurement 点数：%d' % r['measurement']['points'],
              '- 保存：写出 %s 帧，durable=%s' % (r['measurement']['writer']['frames'],
                                                r['measurement']['writer']['durable']),
              '- 质量分布：%s；低质量帧 %d' % (r['quality']['counts'], r['quality']['low_quality_frames']),
              '- 处理耗时：总 %.2f s，几何 p50 %.2f ms/帧，写盘 p50 %.2f ms/帧' % (
                  r['timing']['total_s'], r['timing']['geometry_p50'] * 1000, r['timing']['persist_p50'] * 1000),
              '- 特征发现耗时：%.2f s' % r['feature_discovery_s'], '']
    lines += ['## 3. 自动特征与尺寸', '']
    lines += [fmt_feature(f) for f in feats] if feats else ['- 未产生任何特征记录']
    lines += ['', '（数值来自 SIMULATION_ONLY 几何；不是实物尺寸，未与卡尺比对。）', '']
    lines += ['## 4. 曝光与补采状态机', '',
              '- 最终曝光：lines=%s，revision=%s' % (r['exposure']['final_lines'], r['exposure']['final_revision']),
              '- 曝光事件：%d 次；效果模拟：%s' % (len(r['exposure']['events']), r['exposure']['effect_on_image']),
              '- 补采清单：%d 个 %d° 扇区，执行=%s' % (r['rescan']['actions'], r['rescan']['bin_deg'],
                                                 r['rescan']['executed']),
              '- 说明：' + r['rescan']['reason'], '']
    lines += ['## 5. 显示与保存', '',
              '- 显示：%s，文件 %s' % (r['display'].get('backend'), r['display'].get('files')),
              '- HDMI 设备访问：%s' % r.get('hdmi_device_accessed'), '']
    lines += ['## 6. 独立读回校验', '']
    if v:
        lines += ['- 通过：%s' % v['passed'],
                  '- 帧记录 %d，commit %d，重建点数 %d' % (v['frames_written'], v['commits'], v['points_rebuilt']),
                  '- cloud.npy 与逐帧 XYZ 逐位相同：%s' % v['cloud_identical'],
                  '- 特征支持索引越界：%d' % v['features_with_bad_support'],
                  '- 问题：%s' % (v['problems'] or '无')]
    else:
        lines += ['- 未运行 VERIFY.json']
    lines += ['', '## 7. 边界', '',
              '- physical_accuracy_verified=%s；feature_discovery_included=%s' % (
                  r['physical_accuracy_verified'], r['feature_discovery_included']),
              '- camera_commands=%s，motor_commands=%s，FPGA_loads=%s' % (
                  r['camera_commands'], r['motor_commands'], r['FPGA_loads']), '']
    (run / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return run / 'REPORT.md'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run', type=Path, required=True)
    print(build(p.parse_args().run))


if __name__ == '__main__':
    main()
