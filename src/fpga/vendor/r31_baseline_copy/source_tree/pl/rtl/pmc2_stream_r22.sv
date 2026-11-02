`timescale 1ns/1ps
// PMC2 fixed-threshold extractor. Raw beats remain byte-for-byte identical.
// Double line banks decouple the word-to-pixel gearbox and bounded analysis.
// An overrun discards ALL observations, preserving the same-frame Raw fallback.
module pmc2_stream_r22 #(
    parameter integer FRAME_WIDTH=752, FRAME_HEIGHT=480, MAX_POINTS=3840
) (
    input wire clk, rst, start, abort,
    input wire [1023:0] config_header,
    input wire unsupported_config,
    input wire [31:0] raw_data,
    input wire raw_valid, raw_last,
    output wire raw_ready,
    input wire frame_fault,
    output wire [31:0] tdata,
    output wire tvalid, tlast,
    input wire tready,
    output reg busy, done, error
);
    localparam IDLE=0, SCAN=1, DIVIDE=2, STORE=3, PREFETCH=4;
    localparam WAIT_FRAME=0, H_MID=1, H_MID_WAIT=2, H_LEFT=3,
        H_LEFT_WAIT=4,H_RIGHT=5,H_RIGHT_WAIT=6,H_STORE=7,
        HEADER=8,LOAD=9,LOAD_WAIT=10,WORDS=11,
        H_MID_RAM_WAIT=12,H_LEFT_RAM_WAIT=13,H_RIGHT_RAM_WAIT=14;
    (* ram_style="distributed" *) reg [7:0] pixels [0:2*FRAME_WIDTH-1];
    // 160-bit stored fields, expanded to twelve wire words at serialization.
    (* ram_style="block" *) reg [159:0] records [0:MAX_POINTS-1];
    (* ram_style="block" *) reg [31:0] quality_left [0:MAX_POINTS-1];
    (* ram_style="block" *) reg [31:0] quality_right [0:MAX_POINTS-1];
    (* ram_style="block" *) reg [7:0] hints [0:MAX_POINTS-1];
    reg [11:0] row_offset [0:FRAME_HEIGHT-1];
    reg [3:0] row_count [0:FRAME_HEIGHT-1];
    reg [31:0] header [0:31];
    reg [31:0] gearbox;
    reg [2:0] bytes_left;
    reg gearbox_last, write_bank, read_bank, pending, pending_bank;
    reg [9:0] input_x,input_y,pending_y,scan_y,scan_x,fetch_x;
    reg input_complete;
    reg [31:0] fallback;
    reg [11:0] count;
    reg [3:0] in_row;
    reg [2:0] scan_state;
    reg [3:0] output_state;
    reg run_active, rising, merged, have_peak;
    reg [9:0] first,last,plateau_start,peak_count,saturated;
    reg [7:0] previous,first_value,last_value,maximum,previous_peak,valley;
    reg [17:0] sum0;
    reg [26:0] sum1;
    reg [39:0] quotient,dividend;
    reg [18:0] remainder;
    reg [5:0] div_bit;
    reg [21:0] center;
    reg [3:0] reason;
    reg signed [9:0] contrast2;
    reg [7:0] edge_drop;
    reg [11:0] hint_j, output_index;
    reg [9:0] hint_y;
    reg [3:0] hint_i,hint_k;
    reg [7:0] options;
    reg [31:0] mid_q,left_q,right_q;
    reg [11:0] left_address,right_address,left_offset,right_offset;
    reg [3:0] left_count,right_count;
    reg [31:0] left_read,right_read;
    // Unconditional synchronous read ports allow true BRAM inference. Address
    // registers split row lookup -> candidate lookup into separate clock stages.
    always @(posedge clk) begin
        left_read<=quality_left[left_address];
        right_read<=quality_right[right_address];
    end
    reg [159:0] record_read,output_record;
    reg [7:0] hint_read,output_hint;
    reg [5:0] header_index;
    reg [3:0] word_index;
    reg [31:0] data_reg;
    integer z;
    wire [7:0] threshold=header[22][7:0];
    wire [9:0] x0=header[18][9:0],x1=header[20][9:0];
    wire [9:0] y0=header[19][9:0],y1=header[21][9:0];
    reg [10:0] background_address;
    wire [10:0] row_base=read_bank?FRAME_WIDTH:0;
    wire [10:0] read_address=(scan_state==SCAN || scan_state==PREFETCH)?row_base+fetch_x:background_address;
    wire [7:0] background_read=pixels[read_address];
    reg [7:0] scan_pixel;
    always @(posedge clk) scan_pixel<=pixels[read_address];
    wire [7:0] v=scan_pixel;
    wire bright=scan_x<x1 && v>threshold;
    wire [7:0] weight=v-threshold;
    wire [18:0] trial={remainder[17:0],dividend[39]};
    wire [31:0] normal_count=(fallback==0)?count:0;
    wire [31:0] total_words=32+normal_count*12;
    assign raw_ready=busy && output_state==WAIT_FRAME && bytes_left==0 && !input_complete;
    assign tvalid=busy && (output_state==HEADER || output_state==WORDS);
    assign tdata=data_reg;
    assign tlast=tvalid && ((output_state==HEADER && header_index==31 && normal_count==0) ||
                           (output_state==WORDS && output_index==normal_count-1 && word_index==11));
    function automatic [8:0] median2(input [7:0] a,b,c,input integer n);
        reg [7:0] m;
        begin
            m=(a>b)?((b>c)?b:((a>c)?c:a)):((a>c)?a:((b>c)?c:b));
            median2=(n==1)?{a,1'b0}:((n==2)?({1'b0,a}+b):{m,1'b0});
        end
    endfunction
    function automatic [31:0] packet_word(input [159:0] r,input [7:0] hint,input [3:0] w);
        begin
            case(w)
                0:packet_word={22'd0,r[9:0]};
                1:packet_word={22'd0,r[19:10]};
                2:packet_word={22'd0,r[29:20]};
                3:packet_word={14'd0,r[47:30]};
                4:packet_word={5'd0,r[74:48]};
                5:packet_word={10'd0,r[96:75]};
                6:packet_word={24'd0,r[104:97]};
                7:packet_word={22'd0,r[114:105]};
                8:packet_word={22'd0,r[124:115]};
                9:packet_word={{22{r[134]}},r[134:125]};
                10:packet_word={24'd0,r[142:135]};
                default:packet_word={14'd0,(hint>1),(hint==1),hint,4'd0,r[146:143]};
            endcase
        end
    endfunction
    function automatic [31:0] header_word(input [5:0] w);
        begin
            case(w)
                0:header_word=32'h32434d50;
                1:header_word=32'h00020000;
                2:header_word=128;
                3:header_word=48;
                4:header_word=128+normal_count*48;
                5:header_word=normal_count;
                6:header_word=1|fallback;
                31:header_word=0;
                default:header_word=header[w];
            endcase
        end
    endfunction
    reg [8:0] left_background,right_background,background;
    reg [7:0] bg_l0,bg_l1,bg_l2,bg_r0,bg_r1,bg_r2;
    reg [1:0] bg_ln,bg_rn;
    reg [9:0] width_value;
    reg [7:0] edge_value;
    reg [3:0] computed_reason;
    always @* begin
        width_value=last-first+1;
        edge_value=(maximum-first_value<maximum-last_value)?maximum-first_value:maximum-last_value;
        computed_reason=0;
        if (width_value<header[23] || width_value>header[24]) computed_reason=1;
        else if (sum0<header[25] || maximum-threshold<header[26]) computed_reason=2;
        else if (saturated!=0) computed_reason=3;
        else if (first==x0 || last==x1-1) computed_reason=4;
        else if (first<=x0 || last>=x1-1) computed_reason=7;
        if(merged) computed_reason=10;
    end
    wire signed [32:0] left_delta=$signed({1'b0,left_read[21:0]})-$signed({1'b0,mid_q[21:0]});
    wire signed [32:0] right_delta=$signed({1'b0,right_read[21:0]})-$signed({1'b0,mid_q[21:0]});
    wire signed [33:0] residual=$signed({1'b0,right_read[21:0]})-2*$signed({1'b0,mid_q[21:0]})+$signed({1'b0,left_q[21:0]});
    always @(posedge clk) begin
        done<=0;
        if (rst || abort) begin
            busy<=0;done<=0;error<=0;bytes_left<=0;pending<=0;
            scan_state<=IDLE;output_state<=WAIT_FRAME;input_complete<=0;
            fallback<=0;count<=0;run_active<=0;input_x<=0;input_y<=0;
        end else if(start && !busy) begin
            for(z=0;z<32;z=z+1) header[z]<=config_header[32*z+:32];
            busy<=1;error<=0;bytes_left<=0;pending<=0;scan_state<=IDLE;
            output_state<=WAIT_FRAME;input_complete<=0;count<=0;
            fallback<=unsupported_config?32'd6:0;input_x<=0;input_y<=0;write_bank<=0;
            run_active<=0;
        end else if(busy) begin
            if(frame_fault) fallback<=fallback|32'd18;
            if(raw_valid && raw_ready) begin
                gearbox<=raw_data;bytes_left<=4;gearbox_last<=raw_last;
            end
            if(bytes_left!=0) begin
                pixels[(write_bank?FRAME_WIDTH:0)+input_x]<=gearbox[7:0];
                gearbox<=gearbox>>8;bytes_left<=bytes_left-1;
                if(input_x==FRAME_WIDTH-1) begin
                    input_x<=0;write_bank<=!write_bank;
                    if(input_y<FRAME_HEIGHT-1) input_y<=input_y+1;
                    if(input_y>=y0 && input_y<y1) begin
                        if(pending || (scan_state!=IDLE && read_bank==write_bank)) fallback<=fallback|32'd18;
                        else begin pending<=1;pending_bank<=write_bank;pending_y<=input_y;end
                    end
                end else input_x<=input_x+1;
                if(bytes_left==1 && gearbox_last) begin
                    input_complete<=1;
                    if(input_x!=FRAME_WIDTH-1 || input_y!=FRAME_HEIGHT-1) fallback<=fallback|32'd18;
                end
            end
            case(scan_state)
                IDLE:if(pending) begin
                    pending<=0;read_bank<=pending_bank;scan_y<=pending_y;scan_x<=x0;fetch_x<=x0;
                    in_row<=0;row_offset[pending_y]<=count;row_count[pending_y]<=0;
                    run_active<=0;scan_state<=PREFETCH;
                end
                PREFETCH:begin fetch_x<=fetch_x+1;scan_state<=SCAN;end
                SCAN:begin
                    if(bright) begin
                        if(!run_active) begin
                            run_active<=1;first<=scan_x;last<=scan_x;sum0<=weight;sum1<=weight*scan_x;
                            maximum<=v;first_value<=v;last_value<=v;previous<=v;plateau_start<=scan_x;
                            peak_count<=0;saturated<=v>=header[27];rising<=0;merged<=0;have_peak<=0;valley<=255;
                        end else begin
                            last<=scan_x;sum0<=sum0+weight;sum1<=sum1+weight*scan_x;
                            if(v>maximum) maximum<=v;
                            last_value<=v;if(v>=header[27]) saturated<=saturated+1;
                            if(v!=previous) begin
                                if(v<previous && rising && plateau_start>first) begin
                                    peak_count<=peak_count+1;
                                    if(have_peak && ((previous_peak<previous?previous_peak:previous)-valley)>=header[28][23:16]) merged<=1;
                                    previous_peak<=previous;have_peak<=1;valley<=v;
                                end else if(have_peak && v<valley) valley<=v;
                                rising<=v>previous;plateau_start<=scan_x;
                            end else if(have_peak && v<valley) valley<=v;
                            previous<=v;
                        end
                        scan_x<=scan_x+1;fetch_x<=fetch_x+1;
                    end else if(run_active) begin
                        run_active<=0;dividend<={1'b0,sum1,12'd0};quotient<=0;remainder<=0;div_bit<=40;
                        reason<=computed_reason;edge_drop<=(first<=x0 || last>=x1-1)?0:edge_value;
                        scan_state<=DIVIDE;
                    end else if(scan_x>=x1) begin
                        row_count[scan_y]<=in_row;scan_state<=IDLE;
                    end else begin scan_x<=scan_x+1;fetch_x<=fetch_x+1;end
                end
                DIVIDE:begin
                    // The exact flank medians share the row RAM read port.
                    // Use divider slack for six reads and registered quality
                    // stages; no combinational address/RAM/median/reject chain.
                    case(div_bit)
                        40:begin
                            bg_ln<=(first-x0<3)?first-x0:3;
                            bg_rn<=(x1-last-1<3)?x1-last-1:3;
                            background_address<=row_base+first-((first-x0<3)?first-x0:11'd3);
                        end
                        39:begin bg_l0<=background_read;background_address<=row_base+first-bg_ln+(bg_ln>1?11'd1:11'd0);end
                        38:begin bg_l1<=background_read;background_address<=row_base+first-11'd1;end
                        37:begin bg_l2<=background_read;background_address<=row_base+last+11'd1;end
                        36:begin bg_r0<=background_read;background_address<=row_base+last+(bg_rn>1?11'd2:11'd1);end
                        35:begin bg_r1<=background_read;background_address<=row_base+last+bg_rn;end
                        34:bg_r2<=background_read;
                        33:begin
                            left_background<=median2(bg_l0,bg_l1,bg_l2,bg_ln);
                            right_background<=median2(bg_r0,bg_r1,bg_r2,bg_rn);
                        end
                        32:background<=(left_background>right_background)?left_background:right_background;
                        31:contrast2<=(first<=x0 || last>=x1-1)?0:($signed({2'b0,maximum,1'b0})-$signed({2'b0,background}));
                        30:if(reason==0)begin
                            if(contrast2<2*$signed({1'b0,header[28][7:0]}))reason<=8;
                            else if(width_value<3 || edge_drop<header[28][15:8] || peak_count==0)reason<=9;
                        end
                    endcase
                    dividend<=dividend<<1;quotient<=quotient<<1;
                    if(trial>={1'b0,sum0}) begin remainder<=trial-sum0;quotient[0]<=1;end
                    else remainder<=trial;
                    if(div_bit==1) begin center<={quotient[20:0],(trial>={1'b0,sum0})};scan_state<=STORE;end
                    div_bit<=div_bit-1;
                end
                STORE:begin
                    if(in_row>=8 || count>=MAX_POINTS) fallback<=fallback|32'd10;
                    else begin
                        records[count]<={13'd0,reason,edge_drop,contrast2,peak_count,saturated,maximum,center,sum1,sum0,last,first,scan_y};
                        quality_left[count]<={6'd0,reason,center};quality_right[count]<={6'd0,reason,center};
                        count<=count+1;in_row<=in_row+1;
                    end
                    fetch_x<=scan_x;scan_state<=PREFETCH;
                end
            endcase
            case(output_state)
                WAIT_FRAME:if(input_complete && bytes_left==0 && !pending && scan_state==IDLE) begin
                    hint_j<=0;
                    if(fallback!=0 || count==0) begin header_index<=0;data_reg<=32'h32434d50;output_state<=HEADER;end
                    else output_state<=H_MID;
                end
                H_MID:begin record_read<=records[hint_j];left_address<=hint_j;output_state<=H_MID_RAM_WAIT;end
                H_MID_RAM_WAIT:output_state<=H_MID_WAIT;
                H_MID_WAIT:begin
                    mid_q<=left_read;hint_y<=record_read[9:0];hint_i<=0;hint_k<=0;options<=0;
                    if(left_read[25:22]!=0 || record_read[9:0]<=y0 || record_read[9:0]>=y1-1) output_state<=H_STORE;
                    else begin
                        left_offset<=row_offset[record_read[9:0]-1'b1];right_offset<=row_offset[record_read[9:0]+1'b1];
                        left_count<=row_count[record_read[9:0]-1'b1];right_count<=row_count[record_read[9:0]+1'b1];
                        output_state<=H_LEFT;
                    end
                end
                H_LEFT:begin
                    if(hint_i>=left_count) output_state<=H_STORE;
                    else begin left_address<=left_offset+hint_i;output_state<=H_LEFT_RAM_WAIT;end
                end
                H_LEFT_RAM_WAIT:output_state<=H_LEFT_WAIT;
                H_LEFT_WAIT:begin
                    left_q<=left_read;hint_k<=0;
                    if(left_read[25:22]!=0 || left_delta>$signed(header[29]) || left_delta<-$signed(header[29])) begin hint_i<=hint_i+1;output_state<=H_LEFT;end
                    else output_state<=H_RIGHT;
                end
                H_RIGHT:begin
                    if(hint_k>=right_count) begin hint_i<=hint_i+1;output_state<=H_LEFT;end
                    else begin right_address<=right_offset+hint_k;output_state<=H_RIGHT_RAM_WAIT;end
                end
                H_RIGHT_RAM_WAIT:output_state<=H_RIGHT_WAIT;
                H_RIGHT_WAIT:begin
                    if(right_read[25:22]==0 && right_delta<=$signed(header[29]) && right_delta>=-$signed(header[29]) &&
                       residual<=$signed(header[30]) && residual>=-$signed(header[30])) options<=options+1;
                    hint_k<=hint_k+1;output_state<=H_RIGHT;
                end
                H_STORE:begin
                    hints[hint_j]<=options;
                    if(hint_j==count-1) begin header_index<=0;data_reg<=32'h32434d50;output_state<=HEADER;end
                    else begin hint_j<=hint_j+1;output_state<=H_MID;end
                end
                HEADER:if(tready) begin
                    if(header_index==31) begin
                        if(normal_count==0) begin busy<=0;done<=1;error<=fallback!=0;end
                        else begin output_index<=0;output_state<=LOAD;end
                    end else begin header_index<=header_index+1;data_reg<=header_word(header_index+1);end
                end
                LOAD:begin record_read<=records[output_index];hint_read<=hints[output_index];output_state<=LOAD_WAIT;end
                LOAD_WAIT:begin output_record<=record_read;output_hint<=hint_read;word_index<=0;data_reg<=packet_word(record_read,hint_read,0);output_state<=WORDS;end
                WORDS:if(tready) begin
                    if(word_index==11) begin
                        if(output_index==normal_count-1) begin busy<=0;done<=1;end
                        else begin output_index<=output_index+1;output_state<=LOAD;end
                    end else begin word_index<=word_index+1;data_reg<=packet_word(output_record,output_hint,word_index+1);end
                end
            endcase
        end
    end
endmodule

