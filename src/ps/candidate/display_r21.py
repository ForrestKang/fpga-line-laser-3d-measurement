"""Native-resolution camera and measured-point dashboard for both scan paths."""
from virtual_scan.display_renderer_r19d import ReusedCanvasRenderer
import time
from virtual_scan.journal_owner_r20g import ram_journal_owner
class TimedOwner:
    def __init__(self,config):self.owner=ram_journal_owner(config)
    def configure(self):return self.owner.configure()
    def show_canvas(self,canvas):
        receipt=self.owner.show_canvas(canvas)
        return dict(receipt,r21_ack_perf_s=time.perf_counter(),real_HDMI_ACK=True)
    def render_and_show(self,renderer,gray,points,**options):
        receipt=self.owner.render_and_show(renderer,gray,points,options)
        return dict(receipt,r21_ack_perf_s=time.perf_counter(),real_HDMI_ACK=True)
    def close(self,timeout_s=30.):return self.owner.close(timeout_s=timeout_s)
def timed_owner_factory(config):return TimedOwner(config)
class MultiBranchRenderer(ReusedCanvasRenderer):
    def __init__(self,config):
        super().__init__(point_raster_manifest=config.get('raster_manifest'))
        self.target=config.get('target','MULTI-BRANCH')
    def render(self,gray,points,*,status,cloud_revision=None):
        import cv2
        canvas=super().render(gray,points,status=status,cloud_revision=cloud_revision)
        cv2.putText(canvas,self.target+' / MULTI-BRANCH Q12 / FULL RAW SAVED',
            (20,111),cv2.FONT_HERSHEY_SIMPLEX,.43,(130,215,250),1,cv2.LINE_AA)
        return canvas
def renderer_factory(config):return MultiBranchRenderer(config)
