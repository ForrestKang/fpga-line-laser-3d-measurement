`timescale 1ns/1ps
// SOF event mailbox. Only reset the toggle with the global system reset:
// capture abort/flush must not manufacture a second event by resetting it.
module p2_capture_metadata (
    input wire system_clk, system_resetn, cam_pclk,
    input wire cam_metadata_reset,
    input wire cam_sof_admitted,
    input wire [31:0] cam_request_id,
    input wire [15:0] cam_epoch,
    input wire request_accepted, active, cancelled, complete,
    input wire [31:0] request_id,
    input wire [15:0] capture_epoch,
    input wire [31:0] motor_accepted_id, motor_epoch, motor_emitted,
    input wire [63:0] timestamp_counter,
    output reg metadata_valid, frame_seen,
    output reg [31:0] snapshot_request_id,
    output reg [15:0] snapshot_capture_epoch,
    output reg [31:0] snapshot_motor_id, snapshot_motor_epoch, snapshot_emitted,
    output reg [63:0] snapshot_timestamp,
    output reg [31:0] event_count
);
    // R37: shared global-only camera reset, generated once in capture_core.
    // Capture abort/flush still cannot erase this SOF event mailbox.
    reg cam_toggle;
    reg [31:0] cam_id_mailbox;
    reg [15:0] cam_epoch_mailbox;
    always @(posedge cam_pclk or posedge cam_metadata_reset) begin
        if (cam_metadata_reset) begin
            cam_toggle <= 0; cam_id_mailbox <= 0; cam_epoch_mailbox <= 0;
        end else if (cam_sof_admitted) begin
            cam_toggle <= ~cam_toggle;
            cam_id_mailbox <= cam_request_id;
            cam_epoch_mailbox <= cam_epoch;
        end
    end
    (* ASYNC_REG = "TRUE" *) reg toggle_sync1, toggle_sync2, toggle_sync3;
    reg toggle_seen;
    // Held data have two settling stages. The event uses THREE stages so
    // payload paths bounded to one 10ns system period may arrive after
    // the toggle first stage without being consumed one cycle too early.
    reg [31:0] id_sync1, id_sync2;
    reg [15:0] epoch_sync1, epoch_sync2;
    wire event_pending = toggle_sync3 != toggle_seen;
    always @(posedge system_clk or negedge system_resetn) begin
        if (!system_resetn) begin
            toggle_sync1 <= 0; toggle_sync2 <= 0; toggle_sync3 <= 0; toggle_seen <= 0;
            id_sync1 <= 0; id_sync2 <= 0; epoch_sync1 <= 0; epoch_sync2 <= 0;
            metadata_valid <= 0; frame_seen <= 0;
            snapshot_request_id <= 0; snapshot_capture_epoch <= 0;
            snapshot_motor_id <= 0; snapshot_motor_epoch <= 0;
            snapshot_emitted <= 0; snapshot_timestamp <= 0; event_count <= 0;
        end else begin
            toggle_sync1 <= cam_toggle; toggle_sync2 <= toggle_sync1;
            toggle_sync3 <= toggle_sync2;
            id_sync1 <= cam_id_mailbox; id_sync2 <= id_sync1;
            epoch_sync1 <= cam_epoch_mailbox; epoch_sync2 <= epoch_sync1;
            if (event_pending) toggle_seen <= toggle_sync3;
            if (request_accepted) begin
                metadata_valid <= 0; frame_seen <= 0;
            end else if (cancelled) begin
                metadata_valid <= 0; frame_seen <= 0;
            end else begin
                if (event_pending && active && !frame_seen &&
                    id_sync2 == request_id && epoch_sync2 == capture_epoch) begin
                    frame_seen <= 1;
                    snapshot_request_id <= request_id;
                    snapshot_capture_epoch <= capture_epoch;
                    snapshot_motor_id <= motor_accepted_id;
                    snapshot_motor_epoch <= motor_epoch;
                    snapshot_emitted <= motor_emitted;
                    snapshot_timestamp <= timestamp_counter;
                    event_count <= event_count + 32'd1;
                end
                if (complete && frame_seen) metadata_valid <= 1;
            end
        end
    end
endmodule
