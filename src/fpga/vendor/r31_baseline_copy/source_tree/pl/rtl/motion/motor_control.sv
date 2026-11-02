`timescale 1ns/1ps

module motor_control #(
  parameter integer CLOCK_HZ = 100000000
) (
  (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK",
     X_INTERFACE_PARAMETER = "XIL_INTERFACENAME clk, ASSOCIATED_BUSIF S_AXI, ASSOCIATED_RESET resetn, FREQ_HZ 100000000" *)
  input  logic        clk,
  (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 resetn RST",
     X_INTERFACE_PARAMETER = "XIL_INTERFACENAME resetn, POLARITY ACTIVE_LOW" *)
  input  logic        resetn,

  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWADDR",
     X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 16, FREQ_HZ 100000000, READ_WRITE_MODE READ_WRITE" *)
  input  logic [15:0] s_axi_awaddr,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *)
  input  logic        s_axi_awvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *)
  output wire         s_axi_awready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WDATA" *)
  input  logic [31:0] s_axi_wdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *)
  input  logic [3:0]  s_axi_wstrb,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WVALID" *)
  input  logic        s_axi_wvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WREADY" *)
  output wire         s_axi_wready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BRESP" *)
  output logic [1:0]  s_axi_bresp,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BVALID" *)
  output logic        s_axi_bvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BREADY" *)
  input  logic        s_axi_bready,

  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *)
  input  logic [15:0] s_axi_araddr,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *)
  input  logic        s_axi_arvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *)
  output wire         s_axi_arready,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RDATA" *)
  output logic [31:0] s_axi_rdata,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RRESP" *)
  output logic [1:0]  s_axi_rresp,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RVALID" *)
  output logic        s_axi_rvalid,
  (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RREADY" *)
  input  logic        s_axi_rready,

  output logic        step,
  output logic        direction,
  output logic        enable_n,
  output wire         motor_busy,
  output wire         motor_fault,
  output wire [31:0] telemetry_accepted_id,
  output wire [31:0] telemetry_epoch,
  output wire [31:0] telemetry_emitted
);

  localparam logic [15:0] ADDR_VERSION      = 16'h0000;
  localparam logic [15:0] ADDR_STATUS       = 16'h0004;
  localparam logic [15:0] ADDR_COMMAND      = 16'h0008;
  localparam logic [15:0] ADDR_REQUEST_ID   = 16'h000c;
  localparam logic [15:0] ADDR_PULSE_COUNT  = 16'h0010;
  localparam logic [15:0] ADDR_PERIOD       = 16'h0014;
  localparam logic [15:0] ADDR_HIGH         = 16'h0018;
  localparam logic [15:0] ADDR_DIR_SETUP    = 16'h001c;
  localparam logic [15:0] ADDR_ENABLE_SETUP = 16'h0020;
  localparam logic [15:0] ADDR_LEASE        = 16'h0024;
  localparam logic [15:0] ADDR_TIMEOUT      = 16'h0028;
  localparam logic [15:0] ADDR_DIRECTION    = 16'h002c;
  localparam logic [15:0] ADDR_HEARTBEAT    = 16'h0030;
  localparam logic [15:0] ADDR_EMITTED      = 16'h0034;
  localparam logic [15:0] ADDR_ERROR        = 16'h0038;
  localparam logic [15:0] ADDR_ACCEPTED_ID  = 16'h003c;
  localparam logic [15:0] ADDR_EPOCH        = 16'h0040;
  localparam logic [15:0] ADDR_CLOCK_HZ     = 16'h0044;
  localparam logic [15:0] ADDR_CAPABILITIES = 16'h0048;

  localparam logic [31:0] CMD_ARM    = 32'd1;
  localparam logic [31:0] CMD_DISARM = 32'd2;
  localparam logic [31:0] CMD_START  = 32'd4;
  localparam logic [31:0] CMD_STOP   = 32'd8;
  localparam logic [31:0] CMD_CLEAR  = 32'd16;

  localparam logic [1:0] RESP_OKAY   = 2'b00;
  localparam logic [1:0] RESP_SLVERR = 2'b10;

  localparam logic [31:0] ERR_NONE                = 32'd0;
  localparam logic [31:0] ERR_INVALID_CONFIG     = 32'd1;
  localparam logic [31:0] ERR_NOT_ARMED           = 32'd2;
  localparam logic [31:0] ERR_DUPLICATE_REQUEST  = 32'd3;
  localparam logic [31:0] ERR_HEARTBEAT_TIMEOUT  = 32'd4;
  localparam logic [31:0] ERR_MOTION_TIMEOUT     = 32'd5;
  localparam logic [31:0] ERR_CONFIG_CHANGED_BUSY = 32'd6;
  localparam logic [31:0] ERR_INVALID_COMMAND    = 32'd7;

  typedef enum logic [3:0] {
    ST_IDLE         = 4'd0,
    ST_ARMED        = 4'd1,
    ST_ENABLE_SETUP = 4'd2,
    ST_HIGH         = 4'd3,
    ST_LOW          = 4'd4,
    ST_DONE         = 4'd5,
    ST_CANCELLED    = 4'd6,
    ST_FAULT        = 4'd7
  } state_t;

  state_t state;

  logic aw_full;
  logic [15:0] awaddr_q;
  logic w_full;
  logic [31:0] wdata_q;
  logic [3:0] wstrb_q;
  logic ready_to_arm;
  logic [31:0] pulse_count_cfg;
  logic [31:0] period_cfg;
  logic [31:0] high_cfg;
  logic [31:0] dir_setup_cfg;
  logic [31:0] enable_setup_cfg;
  logic [31:0] lease_cfg;
  logic [31:0] timeout_cfg;
  logic [31:0] direction_cfg;
  logic [31:0] request_id_cfg;
  logic [31:0] heartbeat_reg;
  logic [31:0] emitted_reg;
  logic [31:0] error_reg;
  logic [31:0] accepted_id_reg;
  logic [31:0] epoch_reg;

  logic [31:0] active_count;
  logic [31:0] active_period;
  logic [31:0] active_high;
  logic [31:0] active_low;
  logic [31:0] active_setup;
  logic [31:0] active_lease;
  logic [31:0] active_timeout;
  logic active_direction;
  logic [31:0] phase_count;
  logic [31:0] lease_remaining;
  logic [31:0] motion_remaining;

  logic [31:0] status_word;
  logic [31:0] read_data_comb;
  logic [1:0] read_resp_comb;

  function automatic logic is_config_addr(input logic [15:0] addr);
    begin
      case (addr)
        ADDR_PULSE_COUNT, ADDR_PERIOD, ADDR_HIGH, ADDR_DIR_SETUP,
        ADDR_ENABLE_SETUP, ADDR_LEASE, ADDR_TIMEOUT, ADDR_DIRECTION:
          is_config_addr = 1'b1;
        default: is_config_addr = 1'b0;
      endcase
    end
  endfunction

  function automatic logic config_valid(
    input logic [31:0] count,
    input logic [31:0] period_cycles,
    input logic [31:0] high_cycles,
    input logic [31:0] direction_setup_cycles,
    input logic [31:0] enable_setup_cycles,
    input logic [31:0] lease_cycles,
    input logic [31:0] timeout_cycles,
    input logic [31:0] direction_value
  );
    begin
      config_valid = (count != 32'd0) && (count <= 32'd100000) &&
                     (period_cycles != 32'd0) && (high_cycles != 32'd0) &&
                     (high_cycles < period_cycles) &&
                     (direction_setup_cycles != 32'd0) && (enable_setup_cycles != 32'd0) &&
                     (lease_cycles != 32'd0) && (timeout_cycles != 32'd0) &&
                     (direction_value <= 32'd1);
    end
  endfunction

  wire motion_busy = (state == ST_ENABLE_SETUP) || (state == ST_HIGH) || (state == ST_LOW);
  wire is_armed = (state == ST_ARMED);
  wire is_terminal_fault = (state == ST_FAULT);

  // Raw observations for the outer BSG1 guard.  They intentionally mirror
  // the existing internal state and do not alter the MFP1 register map or
  // pulse-generation state machine.
  assign telemetry_accepted_id = accepted_id_reg;
  assign telemetry_epoch = epoch_reg;
  assign telemetry_emitted = emitted_reg;

  assign motor_busy = motion_busy;
  assign motor_fault = is_terminal_fault;

  wire write_fire = aw_full && w_full && !s_axi_bvalid;
  wire write_aligned = (awaddr_q[1:0] == 2'b00);
  wire write_full_strobe = (wstrb_q == 4'hf);
  wire write_format_ok = write_aligned && write_full_strobe;

  wire write_is_command = write_fire && write_format_ok && (awaddr_q == ADDR_COMMAND);
  wire command_disarm = write_is_command && (wdata_q == CMD_DISARM);
  wire command_stop = write_is_command && (wdata_q == CMD_STOP);
  wire command_clear_ok = write_is_command && (wdata_q == CMD_CLEAR) && !motion_busy;
  wire command_arm_ok = write_is_command && (wdata_q == CMD_ARM) && !motion_busy &&
                        (state == ST_IDLE) && ready_to_arm &&
                        config_valid(pulse_count_cfg, period_cfg, high_cfg, dir_setup_cfg,
                                     enable_setup_cfg, lease_cfg, timeout_cfg, direction_cfg);
  wire command_start_ok = write_is_command && (wdata_q == CMD_START) && !motion_busy &&
                          is_armed && (request_id_cfg != 32'd0) &&
                          (request_id_cfg > accepted_id_reg) &&
                          config_valid(pulse_count_cfg, period_cfg, high_cfg, dir_setup_cfg,
                                       enable_setup_cfg, lease_cfg, timeout_cfg, direction_cfg);

  wire config_write_busy = write_fire && write_format_ok && is_config_addr(awaddr_q) && motion_busy;
  wire heartbeat_active = is_armed || motion_busy;
  wire heartbeat_refresh = write_fire && write_format_ok && (awaddr_q == ADDR_HEARTBEAT) &&
                           (wdata_q != heartbeat_reg) && heartbeat_active;
  wire heartbeat_timeout_now = heartbeat_active && !heartbeat_refresh && (lease_remaining <= 32'd1);
  wire motion_timeout_now = motion_busy && (motion_remaining <= 32'd1);
  wire watchdog_fault_now = heartbeat_timeout_now || motion_timeout_now;

  assign s_axi_awready = !aw_full && !s_axi_bvalid;
  assign s_axi_wready  = !w_full && !s_axi_bvalid;
  assign s_axi_arready = !s_axi_rvalid || s_axi_rready;

  always_comb begin
    status_word = 32'd0;
    status_word[0] = motion_busy;
    status_word[1] = is_armed;
    status_word[2] = (state == ST_DONE);
    status_word[3] = (state == ST_CANCELLED);
    status_word[4] = is_terminal_fault;
    status_word[5] = ready_to_arm;
    status_word[6] = step;
    status_word[7] = enable_n;

    read_data_comb = 32'd0;
    read_resp_comb = RESP_OKAY;
    if (s_axi_araddr[1:0] != 2'b00) begin
      read_resp_comb = RESP_SLVERR;
    end else begin
      case (s_axi_araddr)
        ADDR_VERSION:      read_data_comb = 32'd1296453681;
        ADDR_STATUS:       read_data_comb = status_word;
        ADDR_COMMAND:      read_data_comb = 32'd0;
        ADDR_REQUEST_ID:   read_data_comb = request_id_cfg;
        ADDR_PULSE_COUNT:  read_data_comb = pulse_count_cfg;
        ADDR_PERIOD:       read_data_comb = period_cfg;
        ADDR_HIGH:         read_data_comb = high_cfg;
        ADDR_DIR_SETUP:    read_data_comb = dir_setup_cfg;
        ADDR_ENABLE_SETUP: read_data_comb = enable_setup_cfg;
        ADDR_LEASE:        read_data_comb = lease_cfg;
        ADDR_TIMEOUT:      read_data_comb = timeout_cfg;
        ADDR_DIRECTION:    read_data_comb = direction_cfg;
        ADDR_HEARTBEAT:    read_data_comb = heartbeat_reg;
        ADDR_EMITTED:      read_data_comb = emitted_reg;
        ADDR_ERROR:        read_data_comb = error_reg;
        ADDR_ACCEPTED_ID:  read_data_comb = accepted_id_reg;
        ADDR_EPOCH:        read_data_comb = epoch_reg;
        ADDR_CLOCK_HZ:     read_data_comb = CLOCK_HZ;
        ADDR_CAPABILITIES: read_data_comb = 32'd7;
        default: begin
          read_data_comb = 32'd0;
          read_resp_comb = RESP_SLVERR;
        end
      endcase
    end
  end

  always_ff @(posedge clk) begin
    if (!resetn) begin
      aw_full <= 1'b0;
      awaddr_q <= 16'd0;
      w_full <= 1'b0;
      wdata_q <= 32'd0;
      wstrb_q <= 4'd0;
      s_axi_bresp <= RESP_OKAY;
      s_axi_bvalid <= 1'b0;
      s_axi_rdata <= 32'd0;
      s_axi_rresp <= RESP_OKAY;
      s_axi_rvalid <= 1'b0;

      state <= ST_IDLE;
      ready_to_arm <= 1'b0;
      pulse_count_cfg <= 32'd0;
      period_cfg <= 32'd0;
      high_cfg <= 32'd0;
      dir_setup_cfg <= 32'd0;
      enable_setup_cfg <= 32'd0;
      lease_cfg <= 32'd0;
      timeout_cfg <= 32'd0;
      direction_cfg <= 32'd0;
      request_id_cfg <= 32'd0;
      heartbeat_reg <= 32'd0;
      emitted_reg <= 32'd0;
      error_reg <= ERR_NONE;
      accepted_id_reg <= 32'd0;
      epoch_reg <= 32'd0;

      active_count <= 32'd0;
      active_period <= 32'd0;
      active_high <= 32'd0;
      active_low <= 32'd0;
      active_setup <= 32'd0;
      active_lease <= 32'd0;
      active_timeout <= 32'd0;
      active_direction <= 1'b0;
      phase_count <= 32'd0;
      lease_remaining <= 32'd0;
      motion_remaining <= 32'd0;

      step <= 1'b0;
      direction <= 1'b0;
      enable_n <= 1'b1;
    end else begin
      if (s_axi_bvalid && s_axi_bready)
        s_axi_bvalid <= 1'b0;
      if (s_axi_awvalid && s_axi_awready) begin
        awaddr_q <= s_axi_awaddr;
        aw_full <= 1'b1;
      end
      if (s_axi_wvalid && s_axi_wready) begin
        wdata_q <= s_axi_wdata;
        wstrb_q <= s_axi_wstrb;
        w_full <= 1'b1;
      end

      if (s_axi_rvalid && s_axi_rready)
        s_axi_rvalid <= 1'b0;
      if (s_axi_arvalid && s_axi_arready) begin
        s_axi_rdata <= read_data_comb;
        s_axi_rresp <= read_resp_comb;
        s_axi_rvalid <= 1'b1;
      end

      if (write_fire) begin
        aw_full <= 1'b0;
        w_full <= 1'b0;
        s_axi_bvalid <= 1'b1;
        s_axi_bresp <= RESP_OKAY;

        if (!write_aligned || !write_full_strobe) begin
          s_axi_bresp <= RESP_SLVERR;
        end else begin
          case (awaddr_q)
            ADDR_REQUEST_ID: request_id_cfg <= wdata_q;
            ADDR_PULSE_COUNT: begin
              if (motion_busy) s_axi_bresp <= RESP_SLVERR;
              else pulse_count_cfg <= wdata_q;
            end
            ADDR_PERIOD: begin
              if (motion_busy) s_axi_bresp <= RESP_SLVERR;
              else period_cfg <= wdata_q;
            end
            ADDR_HIGH: begin
              if (motion_busy) s_axi_bresp <= RESP_SLVERR;
              else high_cfg <= wdata_q;
            end
            ADDR_DIR_SETUP: begin
              if (motion_busy) s_axi_bresp <= RESP_SLVERR;
              else dir_setup_cfg <= wdata_q;
            end
            ADDR_ENABLE_SETUP: begin
              if (motion_busy) s_axi_bresp <= RESP_SLVERR;
              else enable_setup_cfg <= wdata_q;
            end
            ADDR_LEASE: begin
              if (motion_busy) s_axi_bresp <= RESP_SLVERR;
              else lease_cfg <= wdata_q;
            end
            ADDR_TIMEOUT: begin
              if (motion_busy) s_axi_bresp <= RESP_SLVERR;
              else timeout_cfg <= wdata_q;
            end
            ADDR_DIRECTION: begin
              if (motion_busy) s_axi_bresp <= RESP_SLVERR;
              else direction_cfg <= wdata_q;
            end
            ADDR_HEARTBEAT: heartbeat_reg <= wdata_q;
            ADDR_COMMAND: begin
              case (wdata_q)
                CMD_DISARM, CMD_STOP: s_axi_bresp <= RESP_OKAY;
                CMD_CLEAR: begin
                  if (motion_busy) begin
                    s_axi_bresp <= RESP_SLVERR;
                    error_reg <= ERR_INVALID_COMMAND;
                  end
                end
                CMD_ARM: begin
                  if (motion_busy || (state != ST_IDLE) || !ready_to_arm) begin
                    s_axi_bresp <= RESP_SLVERR;
                    error_reg <= ERR_INVALID_COMMAND;
                  end else if (!config_valid(pulse_count_cfg, period_cfg, high_cfg, dir_setup_cfg,
                                             enable_setup_cfg, lease_cfg, timeout_cfg, direction_cfg)) begin
                    s_axi_bresp <= RESP_SLVERR;
                    error_reg <= ERR_INVALID_CONFIG;
                  end else begin
                    error_reg <= ERR_NONE;
                  end
                end
                CMD_START: begin
                  if (motion_busy) begin
                    s_axi_bresp <= RESP_SLVERR;
                    error_reg <= ERR_INVALID_COMMAND;
                  end else if (!is_armed) begin
                    s_axi_bresp <= RESP_SLVERR;
                    error_reg <= ERR_NOT_ARMED;
                  end else if ((request_id_cfg == 32'd0) || (request_id_cfg <= accepted_id_reg)) begin
                    s_axi_bresp <= RESP_SLVERR;
                    error_reg <= ERR_DUPLICATE_REQUEST;
                  end else if (!config_valid(pulse_count_cfg, period_cfg, high_cfg, dir_setup_cfg,
                                             enable_setup_cfg, lease_cfg, timeout_cfg, direction_cfg)) begin
                    s_axi_bresp <= RESP_SLVERR;
                    error_reg <= ERR_INVALID_CONFIG;
                  end else begin
                    error_reg <= ERR_NONE;
                  end
                end
                default: begin
                  s_axi_bresp <= RESP_SLVERR;
                  error_reg <= ERR_INVALID_COMMAND;
                end
              endcase
            end
            default: s_axi_bresp <= RESP_SLVERR;
          endcase
        end
      end

      if (config_write_busy) begin
        state <= ST_FAULT;
        error_reg <= ERR_CONFIG_CHANGED_BUSY;
        step <= 1'b0;
        enable_n <= 1'b1;
        phase_count <= 32'd0;
        lease_remaining <= 32'd0;
        motion_remaining <= 32'd0;
      end else if (watchdog_fault_now) begin
        state <= ST_FAULT;
        if (motion_timeout_now)
          error_reg <= ERR_MOTION_TIMEOUT;
        else
          error_reg <= ERR_HEARTBEAT_TIMEOUT;
        step <= 1'b0;
        enable_n <= 1'b1;
        phase_count <= 32'd0;
        lease_remaining <= 32'd0;
        motion_remaining <= 32'd0;
      end else if (command_disarm) begin
        ready_to_arm <= 1'b1;
        step <= 1'b0;
        enable_n <= 1'b1;
        phase_count <= 32'd0;
        lease_remaining <= 32'd0;
        motion_remaining <= 32'd0;
        if (motion_busy) begin
          state <= ST_CANCELLED;
          error_reg <= ERR_NONE;
        end else if (state != ST_FAULT) begin
          state <= ST_IDLE;
          error_reg <= ERR_NONE;
        end
      end else if (command_stop) begin
        step <= 1'b0;
        enable_n <= 1'b1;
        phase_count <= 32'd0;
        lease_remaining <= 32'd0;
        motion_remaining <= 32'd0;
        if (motion_busy) begin
          state <= ST_CANCELLED;
          error_reg <= ERR_NONE;
        end else if (state == ST_ARMED) begin
          state <= ST_IDLE;
        end
      end else if (command_clear_ok) begin
        state <= ST_IDLE;
        step <= 1'b0;
        enable_n <= 1'b1;
        phase_count <= 32'd0;
        lease_remaining <= 32'd0;
        motion_remaining <= 32'd0;
        error_reg <= ERR_NONE;
      end else if (command_arm_ok) begin
        state <= ST_ARMED;
        ready_to_arm <= 1'b0;
        active_lease <= lease_cfg;
        lease_remaining <= lease_cfg;
        motion_remaining <= 32'd0;
        phase_count <= 32'd0;
        error_reg <= ERR_NONE;
        step <= 1'b0;
        enable_n <= 1'b1;
      end else if (command_start_ok) begin
        state <= ST_ENABLE_SETUP;
        accepted_id_reg <= request_id_cfg;
        emitted_reg <= 32'd0;
        epoch_reg <= epoch_reg + 32'd1;
        active_count <= pulse_count_cfg;
        active_period <= period_cfg;
        active_high <= high_cfg;
        active_low <= period_cfg - high_cfg;
        if (dir_setup_cfg > enable_setup_cfg)
          active_setup <= dir_setup_cfg;
        else
          active_setup <= enable_setup_cfg;
        active_lease <= lease_cfg;
        active_timeout <= timeout_cfg;
        active_direction <= direction_cfg[0];
        phase_count <= 32'd0;
        lease_remaining <= lease_cfg;
        motion_remaining <= timeout_cfg;
        direction <= direction_cfg[0];
        step <= 1'b0;
        enable_n <= 1'b0;
        error_reg <= ERR_NONE;
      end else begin
        if (heartbeat_active) begin
          if (heartbeat_refresh)
            lease_remaining <= active_lease;
          else if (lease_remaining > 32'd1)
            lease_remaining <= lease_remaining - 32'd1;
        end
        if (motion_busy && (motion_remaining > 32'd1))
          motion_remaining <= motion_remaining - 32'd1;

        case (state)
          ST_ENABLE_SETUP: begin
            if (phase_count >= (active_setup - 32'd1)) begin
              state <= ST_LOW;
              phase_count <= 32'd0;
            end else begin
              phase_count <= phase_count + 32'd1;
            end
          end
          ST_HIGH: begin
            if (phase_count >= (active_high - 32'd1)) begin
              state <= ST_LOW;
              phase_count <= 32'd0;
              step <= 1'b0;
            end else begin
              phase_count <= phase_count + 32'd1;
            end
          end
          ST_LOW: begin
            if (phase_count >= (active_low - 32'd1)) begin
              phase_count <= 32'd0;
              if (emitted_reg >= active_count) begin
                state <= ST_DONE;
                step <= 1'b0;
                enable_n <= 1'b1;
                lease_remaining <= 32'd0;
                motion_remaining <= 32'd0;
              end else begin
                state <= ST_HIGH;
                step <= 1'b1;
                emitted_reg <= emitted_reg + 32'd1;
              end
            end else begin
              phase_count <= phase_count + 32'd1;
            end
          end
          default: begin
            // IDLE, ARMED, and terminal states only service their watchdog above.
          end
        endcase
      end
    end
  end
endmodule
