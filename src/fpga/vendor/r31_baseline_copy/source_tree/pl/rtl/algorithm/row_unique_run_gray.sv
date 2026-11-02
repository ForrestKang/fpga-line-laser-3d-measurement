`timescale 1ns/1ps
// URC1 camera-domain sideband. The original whole-row producer is unchanged.
// Every run is classified; only the first eligible run needs storage because
// a second eligible run explicitly makes this row unsupported. Its right
// window consumes ORIGINAL pixels, including pixels belonging to later runs.
module row_unique_run_gray #(
    parameter integer FRAME_WIDTH=752, FRAME_HEIGHT=480
)(
    input logic clk, rst, enable, frame_start, pixel_valid,
    input logic [9:0] pixel_x, pixel_y,
    input logic [7:0] gray,
    input logic line_end, line_bad,
    input logic [9:0] line_y,
    input logic [7:0] threshold, min_peak,
    input logic [9:0] roi_x0, roi_x1, roi_y0, roi_y1, min_width, max_width,
    input logic [17:0] min_sum,
    output logic row_valid,
    output logic [9:0] row_y, row_count,
    output logic [63:0] selected_record,
    output wire busy,
    output logic overrun
);
    typedef struct packed {
        logic in_run, seen, bad;
        logic [9:0] seen_x, seen_y;
        logic [3:0] history_count;
        logic [9:0] count;
        logic [3:0] eligible;
        logic [4:0] class_or;
        logic pure_sat;
        logic [17:0] sum;
        logic [26:0] moment;
        logic [7:0] peak, first_gray, last_gray;
        logic [9:0] first, last, saturated;
        logic [10:0] left_sum;
        logic left_complete;
        logic have_selected;
        logic [17:0] selected_sum;
        logic [26:0] selected_moment;
        logic [7:0] selected_peak, selected_first_gray, selected_last_gray;
        logic [9:0] selected_first, selected_last;
        logic [10:0] selected_left_sum, selected_right_sum;
        logic selected_left_complete;
        logic [3:0] selected_right_count;
    } row_state_t;
    row_state_t acc, next_acc;
    logic [7:0] history [0:10];
    logic inside_roi, support, close_run;
    logic [8:0] weight;
    logic [4:0] run_class;
    logic [10:0] left_window;
    integer i;
    always_comb begin
        next_acc=frame_start ? '0 : acc;
        inside_roi=pixel_x>=roi_x0 && pixel_x<roi_x1 &&
                   pixel_y>=roi_y0 && pixel_y<roi_y1;
        support=inside_roi && gray>threshold;
        weight={1'b0,gray}-{1'b0,threshold};
        close_run=0; run_class=0; left_window=0;
        for (integer j=3;j<11;j=j+1) left_window=left_window+history[j];
        if (pixel_valid || line_end) next_acc.bad=next_acc.bad | line_bad;
        if (pixel_valid) begin
            if (next_acc.seen && (pixel_x!=next_acc.seen_x+1'b1 || pixel_y!=next_acc.seen_y))
                next_acc.bad=1;
            next_acc.seen=1; next_acc.seen_x=pixel_x; next_acc.seen_y=pixel_y;
            if (next_acc.history_count<11) next_acc.history_count=next_acc.history_count+1'b1;
            // This independent window is not reset by a later support run.
            if (next_acc.have_selected && inside_roi &&
                {1'b0,pixel_x}>={1'b0,next_acc.selected_last}+11'd4 &&
                {1'b0,pixel_x}<={1'b0,next_acc.selected_last}+11'd11) begin
                next_acc.selected_right_sum=next_acc.selected_right_sum+gray;
                next_acc.selected_right_count=next_acc.selected_right_count+1'b1;
            end
            if (support) begin
                if (!next_acc.in_run) begin
                    if (next_acc.count!=10'h3ff) next_acc.count=next_acc.count+1'b1;
                    next_acc.sum=0; next_acc.moment=0; next_acc.peak=0;
                    next_acc.first=pixel_x; next_acc.saturated=0;
                    next_acc.first_gray=gray;
                    next_acc.left_sum=left_window;
                    // history_count above includes this pixel; use the old
                    // count (and reject SOF) for the eleven previous pixels.
                    next_acc.left_complete=!frame_start && acc.history_count==11 &&
                        {1'b0,pixel_x}>={1'b0,roi_x0}+11'd11;
                end
                next_acc.in_run=1; next_acc.last=pixel_x; next_acc.last_gray=gray;
                next_acc.sum=next_acc.sum+weight;
                next_acc.moment=next_acc.moment+(27'(pixel_x)*27'(weight));
                if (gray>next_acc.peak) next_acc.peak=gray;
                if (gray==255 && next_acc.saturated!=10'h3ff)
                    next_acc.saturated=next_acc.saturated+1'b1;
            end else if (next_acc.in_run) close_run=1;
        end
        if (line_end && next_acc.in_run) close_run=1;
        if (close_run) begin
            if (next_acc.last-next_acc.first+10'd1<min_width ||
                next_acc.last-next_acc.first+10'd1>max_width) run_class[0]=1;
            if (next_acc.sum<min_sum || next_acc.sum==0 ||
                next_acc.peak-threshold<min_peak || next_acc.peak<=threshold) run_class[1]=1;
            if (next_acc.saturated!=0) run_class[2]=1;
            if (next_acc.first==roi_x0 || next_acc.last==roi_x1-1'b1) run_class[3]=1;
            // Native gray8 geometry bounds the honest sums within18/27 bits;
            // malformed coordinates are separately unsupported via bad.
            next_acc.class_or=next_acc.class_or | run_class;
            if (run_class==5'd4) next_acc.pure_sat=1;
            if (run_class==0) begin
                if (next_acc.eligible!=15) next_acc.eligible=next_acc.eligible+1'b1;
                if (!next_acc.have_selected) begin
                    next_acc.have_selected=1;
                    next_acc.selected_sum=next_acc.sum;
                    next_acc.selected_moment=next_acc.moment;
                    next_acc.selected_peak=next_acc.peak;
                    next_acc.selected_first_gray=next_acc.first_gray;
                    next_acc.selected_last_gray=next_acc.last_gray;
                    next_acc.selected_first=next_acc.first;
                    next_acc.selected_last=next_acc.last;
                    next_acc.selected_left_sum=next_acc.left_sum;
                    next_acc.selected_left_complete=next_acc.left_complete;
                    next_acc.selected_right_sum=0; next_acc.selected_right_count=0;
                end
            end
            next_acc.in_run=0;
        end
    end
    logic div_busy;
    logic [5:0] div_count;
    logic [38:0] div_num, div_quot;
    logic [17:0] div_den;
    logic [18:0] div_rem, shifted_rem;
    logic [38:0] shifted_quot;
    logic [63:0] cap_record;
    logic [9:0] cap_y, cap_count;
    logic cap_candidate;
    wire config_supported=threshold==60 && min_peak==15 && min_sum==30 &&
        min_width==1 && max_width==32 && roi_x0<roi_x1 && roi_x1<=FRAME_WIDTH &&
        roi_y0<roi_y1 && roi_y1<=FRAME_HEIGHT;
    wire row_supported=config_supported && !next_acc.bad && next_acc.count<=8 &&
        (next_acc.count<=1 || (next_acc.eligible==1 && !next_acc.pure_sat));
    logic ridge_complete, ridge_contrast, ridge_shape;
    logic [63:0] completed_record;
    always_comb begin
        ridge_complete=next_acc.have_selected && next_acc.selected_left_complete &&
            next_acc.selected_right_count==8 &&
            {1'b0,next_acc.selected_last}+11'd11<{1'b0,roi_x1} && !next_acc.bad;
        ridge_contrast=({4'd0,next_acc.selected_peak}<<3)>=
            ({1'b0,((next_acc.selected_left_sum>next_acc.selected_right_sum) ?
                next_acc.selected_left_sum : next_acc.selected_right_sum)}+12'd160);
        ridge_shape=next_acc.selected_last-next_acc.selected_first+10'd1>=3 &&
            next_acc.selected_peak-next_acc.selected_first_gray>=8 &&
            next_acc.selected_peak-next_acc.selected_last_gray>=8;
        completed_record=0;
        completed_record[63]=row_supported;
        completed_record[62]=next_acc.count>8;
        completed_record[61]=next_acc.pure_sat;
        completed_record[60:57]=next_acc.eligible;
        completed_record[56:52]=next_acc.class_or;
        if (row_supported && next_acc.eligible==1) begin
            completed_record[30]=1;
            completed_record[29:27]=ridge_complete ? {ridge_shape,ridge_contrast,1'b1} : 3'd0;
            completed_record[41:32]=next_acc.selected_first;
            completed_record[51:42]=next_acc.selected_last;
        end
    end
    assign busy=div_busy;
    always_ff @(posedge clk) begin
        if (rst || !enable) begin
            acc<='0; row_valid<=0; row_y<=0; row_count<=0; selected_record<=0;
            div_busy<=0; div_count<=0; div_num<=0; div_quot<=0; div_den<=1; div_rem<=0;
            cap_record<=0; cap_y<=0; cap_count<=0; cap_candidate<=0; overrun<=0;
        end else begin
            row_valid<=0;
            acc<=line_end ? '0 : next_acc;
            if (pixel_valid) begin
                for (i=10;i>0;i=i-1) history[i]<=history[i-1];
                history[0]<=gray;
            end
            if (frame_start) begin
                div_busy<=0; div_count<=0; overrun<=div_busy;
            end else if (div_busy) begin
                shifted_rem={div_rem[17:0],div_num[38]};
                shifted_quot={div_quot[37:0],1'b0};
                if (shifted_rem>={1'b0,div_den}) begin
                    shifted_rem=shifted_rem-{1'b0,div_den}; shifted_quot[0]=1;
                end
                div_rem<=shifted_rem; div_num<={div_num[37:0],1'b0}; div_quot<=shifted_quot;
                if (div_count==38) begin
                    div_busy<=0; div_count<=0; row_valid<=1; row_y<=cap_y; row_count<=cap_count;
                    selected_record<=cap_record;
                    selected_record[21:0]<=cap_candidate ? shifted_quot[21:0] : 22'd0;
                end else div_count<=div_count+1'b1;
            end
            if (line_end) begin
                if (div_busy) overrun<=1;
                else begin
                    cap_record<=completed_record; cap_y<=line_y; cap_count<=next_acc.count;
                    cap_candidate<=completed_record[30];
                    div_num<={next_acc.selected_moment,12'd0};
                    div_den<=next_acc.selected_sum==0 ? 18'd1 : next_acc.selected_sum;
                    div_rem<=0; div_quot<=0; div_count<=0; div_busy<=1;
                end
            end
        end
    end
endmodule
