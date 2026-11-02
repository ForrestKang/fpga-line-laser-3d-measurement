"""Single-owner notebook actions for an already verified paired capture session."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import uuid

from .pair_evidence import export_pair_evidence


class CaptureDashboard:
    def __init__(self, controller, output, *, display=None, parameters=None):
        self.controller, self.display = controller, display
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.parameters = dict(parameters or {})
        self.latest = None
        self.error = None
        self.closed = False
        self._owner = threading.Lock()

    def capture_and_compare(self):
        if self.closed:
            raise RuntimeError('capture session is closed')
        if not self._owner.acquire(blocking=False):
            raise RuntimeError('another capture owns the hardware')
        attempt = self.output/('capture_'+uuid.uuid4().hex)
        attempt.mkdir()
        try:
            result = self.controller.capture_pair()
            try:
                metrics = export_pair_evidence(result, attempt, **self.parameters)
            finally:
                self.controller.drain_results()
            if not metrics['comparison_passed']:
                raise RuntimeError('same-frame comparison failed; raw evidence retained')
            update = None
            if self.display is not None:
                update = self.display.show((attempt/'display_bgr.bin').read_bytes())
            self.latest = {'directory': attempt, 'result': result, 'metrics': metrics,
                           'hdmi_update': update}
            self.error = None
            return self.latest
        except Exception as error:
            self.error = {'type': type(error).__name__, 'message': str(error),
                          'utc': datetime.now(timezone.utc).isoformat(),
                          'last_successful_request': None if self.latest is None else self.latest['result'].request_id}
            (attempt/'failure.json').write_text(json.dumps(self.error,indent=2)+'\n',encoding='utf-8')
            # Preserve the previous successful image and mark it explicitly.
            if self.latest is not None and self.display is not None:
                from PIL import Image, ImageDraw
                import numpy as np
                previous = np.frombuffer((self.latest['directory']/'display_bgr.bin').read_bytes(),np.uint8).reshape(720,1280,3)
                image = Image.fromarray(previous[:,:,::-1])
                draw = ImageDraw.Draw(image)
                draw.rectangle((0,650,1279,719),fill='#501818')
                draw.text((20,665),f"LAST SUCCESSFUL FRAME request={self.latest['result'].request_id} | NEW CAPTURE FAILED",fill='white')
                draw.text((20,690),str(error)[:170],fill='white')
                try:
                    self.display.show(np.asarray(image)[:,:,::-1].tobytes())
                except Exception as display_error:
                    self.error['display_error'] = str(display_error)
                    (attempt/'failure.json').write_text(json.dumps(self.error,indent=2)+'\n',encoding='utf-8')
            raise
        finally:
            self._owner.release()

    def save_result(self):
        if self.latest is None:
            raise RuntimeError('no successful capture to save')
        marker = self.latest['directory']/'user_saved.json'
        marker.write_text(json.dumps({'request_id':self.latest['result'].request_id,
                                     'saved_utc':datetime.now(timezone.utc).isoformat(),
                                     'raw_sha256':self.latest['metrics']['raw_sha256']},indent=2)+'\n',encoding='utf-8')
        return self.latest['directory']

    def close_display(self):
        if not self._owner.acquire(blocking=False):
            raise RuntimeError('capture is in progress')
        try:
            if self.display is not None:
                self.display.close()  # Only release CMA after confirmed VDMA halt.
                self.display = None
        finally:
            self._owner.release()

    def close(self):
        self.close_display()
        self.controller.close()
        if self.controller.recorder is not None:
            self.controller.recorder.close()
        self.closed = True

    def widgets(self):
        import ipywidgets as widgets
        from IPython.display import display, Image
        output = widgets.Output()
        capture = widgets.Button(description='采集并比对',button_style='primary')
        save = widgets.Button(description='保存结果')
        close = widgets.Button(description='关闭显示')
        def act(operation):
            def callback(_):
                capture.disabled = save.disabled = close.disabled = True
                with output:
                    output.clear_output(wait=True)
                    try:
                        value = operation()
                        if isinstance(value, dict) and 'metrics' in value:
                            print(json.dumps(value['metrics'],ensure_ascii=False,indent=2))
                            preview = value['directory']/'overlay_canvas.png'
                            if not preview.is_file():
                                previews=list(value['directory'].glob('overlay*.png'))
                                preview=previews[0] if previews else None
                            if preview is not None and preview.is_file(): display(Image(filename=str(preview)))
                        else: print(value)
                    except Exception as error:
                        print('采集/显示错误：'+str(error))
                        if self.latest is not None: print('保留上次成功帧：'+str(self.latest['result'].request_id))
                    finally:
                        capture.disabled = save.disabled = close.disabled = False
            return callback
        capture.on_click(act(self.capture_and_compare))
        save.on_click(act(self.save_result))
        close.on_click(act(self.close_display))
        return widgets.VBox([widgets.HBox([capture,save,close]),output])
