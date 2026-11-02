`timescale 1ns/1ps

// BSG1 is the fail-closed permit around the MFP1 one-shot motor controller.
// The motor controller remains responsible for pulse generation.  This block
// owns the host lease, task deadline, field-power interlock, and the external
// STEP/EN/heartbeat pins.
module b_motion_guard #(
  parameter integer CLOCK_HZ = 100000000,
  parameter integer CARRIER_MODE = 0,
  parameter integer HEARTBEAT_HALF_CYCLES = 500000,
  // These defaults are the fixed BSG1 contract.  Directed simulation may
  // lower them explicitly; the production BD leaves the defaults untouched.
  parameter integer LEASE_MIN_CYCLES = 1000000,
  parameter integer LEASE_MAX_CYCLES = 100000000
) (
  (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK",
     X_INTERFACE_PARAMETER = "XIL_INTERFACENAME clk, ASSOCIATED_BUSIF S_AXI, ASSOCIATED_RESET resetn, FREQ_HZ 100000000" *)
  input  logic clk,
  (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 resetn RST",
     X_INTERFACE_PARAMETER = "XIL_INTERFACENAME resetn, POLARITY ACTIVE_LOW" *)
  input  logic resetn,

  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWADDR",
     X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 16, FREQ_HZ 100000000, READ_WRITE_MODE READ_WRITE" *)
  input  logic [15:0] s_axi_awaddr,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *)
  input  logic s_axi_awvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *)
  output wire s_axi_awready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WDATA" *)
  input  logic [31:0] s_axi_wdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *)
  input  logic [3:0] s_axi_wstrb,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WVALID" *)
  input  logic s_axi_wvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WREADY" *)
  output wire s_axi_wready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BRESP" *)
  output logic [1:0] s_axi_bresp,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BVALID" *)
  output logic s_axi_bvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BREADY" *)
  input logic s_axi_bready,

  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *)
  input logic [15:0] s_axi_araddr,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *)
  input logic s_axi_arvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *)
  output wire s_axi_arready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RDATA" *)
  output logic [31:0] s_axi_rdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RRESP" *)
  output logic [1:0] s_axi_rresp,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RVALID" *)
  output logic s_axi_rvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RREADY" *)
  input logic s_axi_rready,

  // Raw MFP1 observations and pulse outputs.  These are intentionally kept
  // outside the MFP1 register map so the existing software contract remains.
  input logic motor_step,
  input logic motor_direction,
  input logic motor_busy,
  input logic motor_fault,
  input logic field_power_good,

  output wire step,
  output wire direction,
  output wire enable_n,
  output wire enable_request,
  output wire heartbeat,
  output wire active,
  output wire fault
);

  localparam integer DIRECT = 0;
  localparam integer EXPANSION = 1;

  localparam logic [15:0] ADDR_VERSION       = 16'h0000;
  localparam logic [15:0] ADDR_STATUS        = 16'h0004;
  localparam logic [15:0] ADDR_COMMAND       = 16'h0008;
  localparam logic [15:0] ADDR_REQUEST_ID    = 16'h000c;
  localparam logic [15:0] ADDR_HEARTBEAT     = 16'h0010;
  localparam logic [15:0] ADDR_LEASE_CYCLES  = 16'h0014;
  localparam logic [15:0] ADDR_TIMEOUT_MS    = 16'h0018;
  localparam logic [15:0] ADDR_ERROR         = 16'h001c;
  localparam logic [15:0] ADDR_ACCEPTED_ID   = 16'h0020;
  localparam logic [15:0] ADDR_CARRIER_MODE  = 16'h0024;
  localparam logic [15:0] ADDR_HEARTBEAT_HALF = 16'h0028;
  localparam logic [15:0] ADDR_CLOCK_HZ      = 16'h002c;
  localparam logic [15:0] ADDR_CAPABILITIES  = 16'h0030;

  localparam logic [31:0] VERSION = 32'h42534731;
  localparam logic [31:0] CMD_ARM = 32'd1;
  localparam logic [31:0] CMD_DISARM = 32'd2;
  localparam logic [31:0] CMD_STOP = 32'd4;
  localparam logic [31:0] CMD_CLEAR = 32'd8;

  localparam logic [31:0] ERR_NONE = 32'd0;
  localparam logic [31:0] ERR_INVALID_CONFIG = 32'd1;
  localparam logic [31:0] ERR_LEASE_EXPIRED = 32'd2;
  localparam logic [31:0] ERR_CANCELLED = 32'd3;
  localparam logic [31:0] ERR_TASK_TIMEOUT = 32'd4;
  localparam logic [31:0] ERR_MOTOR_FAULT = 32'd5;
  localparam logic [31:0] ERR_FIELD_POWER_LOST = 32'd6;
  localparam logic [31:0] ERR_DUPLICATE_REQUEST = 32'd7;
  localparam logic [31:0] ERR_INVALID_COMMAND = 32'd8;

  localparam logic [1:0] RESP_OKAY = 2'b00;
  localparam logic [1:0] RESP_SLVERR = 2'b10;
  localparam logic [63:0] CYCLES_PER_MS = (CLOCK_HZ / 1000);

  logic aw_full_q;
  logic [15:0] awaddr_q;
  logic w_full_q;
  logic [31:0] wdata_q;
  logic [3:0] wstrb_q;

  logic [31:0] request_id_q;
  logic [31:0] heartbeat_q;
  logic [31:0] lease_cycles_q;
  logic [31:0] timeout_ms_q;
  logic [31:0] accepted_id_q;
  logic [31:0] error_q;
  logic active_q;
  logic fault_q;
  logic fresh_heartbeat_q;
  logic heartbeat_pin_q;
  logic [63:0] lease_remaining_q;
  logic [63:0] task_remaining_q;
  logic [63:0] heartbeat_counter_q;

  (* ASYNC_REG = "TRUE" *) logic field_power_meta_q;
  (* ASYNC_REG = "TRUE" *) logic field_power_sync_q;

  logic [31:0] status_word;
  logic [31:0] read_data_comb;
  logic [1:0] read_resp_comb;

  function automatic logic address_known(input logic [15:0] address);
    begin
      case (address)
        ADDR_VERSION, ADDR_STATUS, ADDR_COMMAND, ADDR_REQUEST_ID,
        ADDR_HEARTBEAT, ADDR_LEASE_CYCLES, ADDR_TIMEOUT_MS, ADDR_ERROR,
        ADDR_ACCEPTED_ID, ADDR_CARRIER_MODE, ADDR_HEARTBEAT_HALF,
        ADDR_CLOCK_HZ, ADDR_CAPABILITIES: address_known = 1'b1;
        default: address_known = 1'b0;
      endcase
    end
  endfunction

  function automatic logic address_read_only(input logic [15:0] address);
    begin
      case (address)
        ADDR_VERSION, ADDR_STATUS, ADDR_ERROR, ADDR_ACCEPTED_ID,
        ADDR_CARRIER_MODE, ADDR_HEARTBEAT_HALF, ADDR_CLOCK_HZ,
        ADDR_CAPABILITIES: address_read_only = 1'b1;
        default: address_read_only = 1'b0;
      endcase
    end
  endfunction

  function automatic logic valid_config(input logic [31:0] lease_cycles,
                                        input logic [31:0] timeout_ms);
    begin
      valid_config = (lease_cycles >= LEASE_MIN_CYCLES) &&
                     (lease_cycles <= LEASE_MAX_CYCLES) &&
                     (timeout_ms >= 32'd1000) && (timeout_ms <= 32'd300000);
    end
  endfunction

  function automatic [63:0] ms_to_cycles(input logic [31:0] milliseconds);
    begin
      ms_to_cycles = $unsigned(milliseconds) * CYCLES_PER_MS;
    end
  endfunction

  wire field_power_good_ok = (CARRIER_MODE == EXPANSION) ? field_power_sync_q : 1'b1;
  wire safe_permit = active_q && !fault_q;
  wire write_fire = aw_full_q && w_full_q && !s_axi_bvalid;
  wire write_aligned = (awaddr_q[1:0] == 2'b00);
  wire write_full_strobe = (wstrb_q == 4'hf);
  wire write_known = address_known(awaddr_q);
  wire write_read_only = address_read_only(awaddr_q);
  wire write_valid = write_aligned && write_full_strobe && write_known && !write_read_only;
  wire write_command = write_fire && write_valid && (awaddr_q == ADDR_COMMAND);
  wire heartbeat_refresh = write_fire && write_valid && (awaddr_q == ADDR_HEARTBEAT) &&
                           (wdata_q != heartbeat_q);
  wire lease_expired_now = active_q && !heartbeat_refresh && (lease_remaining_q <= 64'd1);
  wire task_expired_now = active_q && (task_remaining_q <= 64'd1);
  wire safety_fault_now = active_q &&
                          (motor_fault ||
                           ((CARRIER_MODE == EXPANSION) && !field_power_good_ok) ||
                           lease_expired_now || task_expired_now);

  assign s_axi_awready = !aw_full_q && !s_axi_bvalid;
  assign s_axi_wready = !w_full_q && !s_axi_bvalid;
  assign s_axi_arready = !s_axi_rvalid || s_axi_rready;

  assign step = safe_permit && motor_step;
  assign direction = motor_direction;
  assign enable_n = (CARRIER_MODE == DIRECT) ? !safe_permit : 1'b1;
  assign enable_request = (CARRIER_MODE == EXPANSION) ? safe_permit : 1'b0;
  assign heartbeat = safe_permit && fresh_heartbeat_q && (lease_remaining_q != 64'd0) ?
                     heartbeat_pin_q : 1'b0;
  assign active = active_q;
  assign fault = fault_q;

  always_comb begin
    status_word = 32'd0;
    status_word[0] = active_q;
    status_word[1] = fault_q;
    status_word[2] = heartbeat;
    status_word[3] = motor_busy;
    status_word[4] = step;
    status_word[5] = (CARRIER_MODE == DIRECT) ? enable_n : enable_request;
    status_word[6] = field_power_good_ok;

    read_data_comb = 32'd0;
    read_resp_comb = RESP_OKAY;
    if (s_axi_araddr[1:0] != 2'b00) begin
      read_resp_comb = RESP_SLVERR;
    end else begin
      case (s_axi_araddr)
        ADDR_VERSION:        read_data_comb = VERSION;
        ADDR_STATUS:         read_data_comb = status_word;
        ADDR_COMMAND:        read_data_comb = 32'd0;
        ADDR_REQUEST_ID:     read_data_comb = request_id_q;
        ADDR_HEARTBEAT:      read_data_comb = heartbeat_q;
        ADDR_LEASE_CYCLES:   read_data_comb = lease_cycles_q;
        ADDR_TIMEOUT_MS:     read_data_comb = timeout_ms_q;
        ADDR_ERROR:          read_data_comb = error_q;
        ADDR_ACCEPTED_ID:    read_data_comb = accepted_id_q;
        ADDR_CARRIER_MODE:   read_data_comb = CARRIER_MODE;
        ADDR_HEARTBEAT_HALF: read_data_comb = HEARTBEAT_HALF_CYCLES;
        ADDR_CLOCK_HZ:       read_data_comb = CLOCK_HZ;
        ADDR_CAPABILITIES:   read_data_comb = 32'd7;
        default: begin
          read_data_comb = 32'd0;
          read_resp_comb = RESP_SLVERR;
        end
      endcase
    end
  end

  // The field-power input is asynchronous to the AXI/motor clock.  Reset
  // asserts both flops low and deasserts synchronously through the two stages;
  // direct-wire builds intentionally ignore this input and report PGOOD=1.
  always_ff @(posedge clk or negedge resetn) begin
    if (!resetn) begin
      field_power_meta_q <= 1'b0;
      field_power_sync_q <= 1'b0;
    end else begin
      field_power_meta_q <= field_power_good;
      field_power_sync_q <= field_power_meta_q;
    end
  end

  always_ff @(posedge clk) begin
    if (!resetn) begin
      aw_full_q <= 1'b0;
      awaddr_q <= 16'd0;
      w_full_q <= 1'b0;
      wdata_q <= 32'd0;
      wstrb_q <= 4'd0;
      s_axi_bresp <= RESP_OKAY;
      s_axi_bvalid <= 1'b0;
      s_axi_rdata <= 32'd0;
      s_axi_rresp <= RESP_OKAY;
      s_axi_rvalid <= 1'b0;

      request_id_q <= 32'd0;
      heartbeat_q <= 32'd0;
      lease_cycles_q <= 32'd0;
      timeout_ms_q <= 32'd0;
      accepted_id_q <= 32'd0;
      error_q <= ERR_NONE;
      active_q <= 1'b0;
      fault_q <= 1'b0;
      fresh_heartbeat_q <= 1'b0;
      heartbeat_pin_q <= 1'b0;
      lease_remaining_q <= 64'd0;
      task_remaining_q <= 64'd0;
      heartbeat_counter_q <= 64'd0;
    end else begin
      if (s_axi_bvalid && s_axi_bready)
        s_axi_bvalid <= 1'b0;
      if (s_axi_awvalid && s_axi_awready) begin
        awaddr_q <= s_axi_awaddr;
        aw_full_q <= 1'b1;
      end
      if (s_axi_wvalid && s_axi_wready) begin
        wdata_q <= s_axi_wdata;
        wstrb_q <= s_axi_wstrb;
        w_full_q <= 1'b1;
      end

      if (s_axi_rvalid && s_axi_rready)
        s_axi_rvalid <= 1'b0;
      if (s_axi_arvalid && s_axi_arready) begin
        s_axi_rdata <= read_data_comb;
        s_axi_rresp <= read_resp_comb;
        s_axi_rvalid <= 1'b1;
      end

      if (write_fire) begin
        aw_full_q <= 1'b0;
        w_full_q <= 1'b0;
        s_axi_bvalid <= 1'b1;
        s_axi_bresp <= write_valid ? RESP_OKAY : RESP_SLVERR;

        if (write_valid) begin
          case (awaddr_q)
            ADDR_REQUEST_ID: begin
              if (active_q)
                s_axi_bresp <= RESP_SLVERR;
              else
                request_id_q <= wdata_q;
            end
            ADDR_HEARTBEAT: begin
              heartbeat_q <= wdata_q;
              if (wdata_q != heartbeat_q) begin
                fresh_heartbeat_q <= 1'b1;
                if (active_q)
                  lease_remaining_q <= lease_cycles_q;
              end
            end
            ADDR_LEASE_CYCLES: begin
              if (active_q)
                s_axi_bresp <= RESP_SLVERR;
              else
                lease_cycles_q <= wdata_q;
            end
            ADDR_TIMEOUT_MS: begin
              if (active_q)
                s_axi_bresp <= RESP_SLVERR;
              else
                timeout_ms_q <= wdata_q;
            end
            default: begin
              // COMMAND is handled below.  Other legal addresses are RO and
              // were rejected by write_valid before reaching this case.
            end
          endcase
        end
      end

      // External interlocks and both finite watchdogs are higher priority than
      // host commands.  A simultaneous fault and DISARM therefore still
      // leaves a sticky diagnostic and safe pins.
      if (safety_fault_now) begin
        active_q <= 1'b0;
        fault_q <= 1'b1;
        fresh_heartbeat_q <= 1'b0;
        heartbeat_pin_q <= 1'b0;
        heartbeat_counter_q <= 64'd0;
        lease_remaining_q <= 64'd0;
        task_remaining_q <= 64'd0;
        if (motor_fault)
          error_q <= ERR_MOTOR_FAULT;
        else if ((CARRIER_MODE == EXPANSION) && !field_power_good_ok)
          error_q <= ERR_FIELD_POWER_LOST;
        else if (task_expired_now)
          error_q <= ERR_TASK_TIMEOUT;
        else
          error_q <= ERR_LEASE_EXPIRED;
      end else if (write_command) begin
        case (wdata_q)
          CMD_ARM: begin
            if (active_q || motor_busy) begin
              s_axi_bresp <= RESP_SLVERR;
              error_q <= ERR_INVALID_COMMAND;
            end else if (fault_q) begin
              s_axi_bresp <= RESP_SLVERR;
              // Keep the originating sticky-fault diagnostic until a
              // permitted CLEAR succeeds.
            end else if (motor_fault) begin
              s_axi_bresp <= RESP_SLVERR;
              error_q <= ERR_MOTOR_FAULT;
            end else if ((CARRIER_MODE == EXPANSION) && !field_power_good_ok) begin
              s_axi_bresp <= RESP_SLVERR;
              error_q <= ERR_FIELD_POWER_LOST;
            end else if (!valid_config(lease_cycles_q, timeout_ms_q) || !fresh_heartbeat_q) begin
              s_axi_bresp <= RESP_SLVERR;
              error_q <= ERR_INVALID_CONFIG;
            end else if (request_id_q == 32'd0 || request_id_q <= accepted_id_q) begin
              s_axi_bresp <= RESP_SLVERR;
              error_q <= ERR_DUPLICATE_REQUEST;
            end else begin
              active_q <= 1'b1;
              accepted_id_q <= request_id_q;
              lease_remaining_q <= lease_cycles_q;
              task_remaining_q <= ms_to_cycles(timeout_ms_q);
              heartbeat_pin_q <= 1'b0;
              heartbeat_counter_q <= 64'd0;
              error_q <= ERR_NONE;
            end
          end
          CMD_DISARM: begin
            active_q <= 1'b0;
            fresh_heartbeat_q <= 1'b0;
            heartbeat_pin_q <= 1'b0;
            heartbeat_counter_q <= 64'd0;
            lease_remaining_q <= 64'd0;
            task_remaining_q <= 64'd0;
            if (!fault_q)
              error_q <= ERR_NONE;
          end
          CMD_STOP: begin
            active_q <= 1'b0;
            fresh_heartbeat_q <= 1'b0;
            heartbeat_pin_q <= 1'b0;
            heartbeat_counter_q <= 64'd0;
            lease_remaining_q <= 64'd0;
            task_remaining_q <= 64'd0;
            if (!fault_q)
              error_q <= ERR_CANCELLED;
          end
          CMD_CLEAR: begin
            if (active_q || motor_busy || motor_fault ||
                ((CARRIER_MODE == EXPANSION) && !field_power_good_ok)) begin
              s_axi_bresp <= RESP_SLVERR;
              if (!fault_q)
                error_q <= ERR_INVALID_COMMAND;
            end else begin
              fault_q <= 1'b0;
              error_q <= ERR_NONE;
              fresh_heartbeat_q <= 1'b0;
            end
          end
          default: begin
            s_axi_bresp <= RESP_SLVERR;
            if (!fault_q)
              error_q <= ERR_INVALID_COMMAND;
          end
        endcase
      end else if (active_q) begin
        if (heartbeat_refresh) begin
          lease_remaining_q <= lease_cycles_q;
        end else if (lease_remaining_q > 64'd0) begin
          lease_remaining_q <= lease_remaining_q - 64'd1;
        end
        if (task_remaining_q > 64'd0)
          task_remaining_q <= task_remaining_q - 64'd1;

        if (heartbeat_counter_q >= HEARTBEAT_HALF_CYCLES - 1) begin
          heartbeat_counter_q <= 64'd0;
          heartbeat_pin_q <= ~heartbeat_pin_q;
        end else begin
          heartbeat_counter_q <= heartbeat_counter_q + 64'd1;
        end
      end else begin
        heartbeat_pin_q <= 1'b0;
        heartbeat_counter_q <= 64'd0;
      end
    end
  end
endmodule
