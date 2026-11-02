`timescale 1ns/1ps

// MT9V034 two-wire serial controller.
//
// The interface is open-drain: drive_low=1 pulls a wire low and drive_low=0
// releases it. The caller supplies the resolved IOBUF input levels.
// Transfer format is 7-bit device address, 8-bit register address, and
// 16-bit MSB-first register data. One quarter-period scheduling quantum is
// used for data setup/hold; SCL low and high intervals each span two quanta.
module mt9v034_sccb_master #(
    parameter integer SYS_CLK_HZ = 100_000_000,
    parameter integer I2C_HZ = 100_000,
    parameter integer STRETCH_TIMEOUT_US = 1000
) (
    input  wire        clk,
    input  wire        resetn,
    input  wire        start,
    input  wire        read_not_write,
    input  wire [6:0]  device_address,
    input  wire [7:0]  register_address,
    input  wire [15:0] write_data,
    input  wire        scl_i,
    input  wire        sda_i,
    output reg         scl_drive_low,
    output reg         sda_drive_low,
    output reg         busy,
    output reg         done,
    output reg         ack_error,
    output reg         timeout_error,
    output reg  [15:0] read_data,
    output wire [7:0]  state_debug
);
    // Ceiling division ensures a non-integral requested rate never runs
    // faster than requested. At 100 MHz / 100 kHz, one quantum is 250 clocks
    // or 2.5 us; each SCL low/high interval is two quanta (5 us).
    localparam integer QUANTUM_DEN = I2C_HZ * 4;
    localparam integer QUANTUM_DIV_RAW =
        (SYS_CLK_HZ + QUANTUM_DEN - 1) / QUANTUM_DEN;
    localparam integer QUANTUM_DIV = (QUANTUM_DIV_RAW < 2) ? 2 : QUANTUM_DIV_RAW;
    localparam integer QUANTUM_WIDTH = (QUANTUM_DIV <= 2) ? 1 : $clog2(QUANTUM_DIV);
    localparam integer HIGH_CYCLES_RAW =
        (SYS_CLK_HZ + (2 * I2C_HZ) - 1) / (2 * I2C_HZ);
    localparam integer HIGH_CYCLES = (HIGH_CYCLES_RAW < 2) ? 2 : HIGH_CYCLES_RAW;
    localparam integer HIGH_HALF_CYCLES = HIGH_CYCLES / 2;
    localparam integer LOW_CYCLES = HIGH_CYCLES;
    localparam integer DATA_HOLD_CYCLES_RAW =
        (SYS_CLK_HZ + 1_999_999) / 2_000_000;
    localparam integer DATA_HOLD_CYCLES =
        (DATA_HOLD_CYCLES_RAW < 1) ? 1 : DATA_HOLD_CYCLES_RAW;
    localparam integer TIMEOUT_CYCLES_RAW =
        ((SYS_CLK_HZ / 1_000_000) * STRETCH_TIMEOUT_US) +
        ((((SYS_CLK_HZ % 1_000_000) * STRETCH_TIMEOUT_US) + 999_999) / 1_000_000);
    localparam integer TIMEOUT_CYCLES =
        (TIMEOUT_CYCLES_RAW < 1) ? 1 : TIMEOUT_CYCLES_RAW;
    localparam integer TIMEOUT_WIDTH = $clog2(TIMEOUT_CYCLES + 1);
    localparam integer HIGH_WIDTH = $clog2(HIGH_CYCLES + 1);
    localparam integer TIMEOUT_QUANTA_RAW =
        ((I2C_HZ * 4 * STRETCH_TIMEOUT_US) + 999_999) / 1_000_000;
    localparam integer TIMEOUT_QUANTA =
        (TIMEOUT_QUANTA_RAW < 1) ? 1 : TIMEOUT_QUANTA_RAW;
    // Kept as a named quarter-tick budget for the idle bus-free wait below.
    localparam integer TIMEOUT_TICKS = TIMEOUT_QUANTA;

    localparam [3:0] ST_IDLE    = 4'd0;
    localparam [3:0] ST_START   = 4'd1;
    localparam [3:0] ST_SEND    = 4'd2;
    localparam [3:0] ST_ACK     = 4'd3;
    localparam [3:0] ST_RESTART = 4'd4;
    localparam [3:0] ST_READ    = 4'd5;
    localparam [3:0] ST_MACK    = 4'd6;
    localparam [3:0] ST_STOP    = 4'd7;
    localparam [3:0] ST_ABORT   = 4'd8;

    reg [QUANTUM_WIDTH-1:0] quantum_count;
    reg quantum_tick;
    reg start_d;
    reg start_pending;
    reg [3:0] state;
    reg [2:0] phase;
    reg [2:0] bit_index;
    reg [2:0] transaction_step;
    reg [7:0] tx_byte;
    reg [7:0] rx_byte;
    reg latched_read;
    reg [6:0] latched_device_address;
    reg [7:0] latched_register_address;
    reg [15:0] latched_write_data;
    reg reading_low_byte;
    reg ack_received;
    reg [TIMEOUT_WIDTH-1:0] stretch_count;
    reg [HIGH_WIDTH-1:0] high_cycle_count;
    reg [HIGH_WIDTH-1:0] low_cycle_count;
    reg [1:0] interval_count;

    assign state_debug = {transaction_step[2:0], phase[1:0], state[2:0]};

    task automatic fail_timeout;
        begin
            timeout_error <= 1'b1;
            scl_drive_low <= 1'b0;
            sda_drive_low <= 1'b0;
            state <= ST_ABORT;
            phase <= 3'd0;
            low_cycle_count <= {HIGH_WIDTH{1'b0}};
        end
    endtask

    task automatic finish_send_bit;
        begin
            scl_drive_low <= 1'b1;
            low_cycle_count <= {HIGH_WIDTH{1'b0}};
            if (bit_index == 3'd0) begin
                phase <= 3'd0;
                state <= ST_ACK;
            end else begin
                bit_index <= bit_index - 1'b1;
                phase <= 3'd0;
            end
        end
    endtask

    task automatic finish_ack_pulse;
        begin
            scl_drive_low <= 1'b1;
            low_cycle_count <= {HIGH_WIDTH{1'b0}};
            if (!ack_received) begin
                state <= ST_STOP;
                phase <= 3'd0;
                interval_count <= 2'd0;
            end else begin
                case (transaction_step)
                    3'd0: begin
                        tx_byte <= latched_register_address;
                        transaction_step <= 3'd1;
                        bit_index <= 3'd7;
                        state <= ST_SEND;
                        phase <= 3'd0;
                    end
                    3'd1: begin
                        transaction_step <= 3'd2;
                        bit_index <= 3'd7;
                        if (latched_read) begin
                            state <= ST_RESTART;
                            phase <= 3'd0;
                            interval_count <= 2'd0;
                        end else begin
                            tx_byte <= latched_write_data[15:8];
                            state <= ST_SEND;
                            phase <= 3'd0;
                        end
                    end
                    3'd2: begin
                        if (latched_read) begin
                            reading_low_byte <= 1'b0;
                            rx_byte <= 8'd0;
                            bit_index <= 3'd7;
                            state <= ST_READ;
                            phase <= 3'd0;
                        end else begin
                            tx_byte <= latched_write_data[7:0];
                            bit_index <= 3'd7;
                            transaction_step <= 3'd3;
                            state <= ST_SEND;
                            phase <= 3'd0;
                        end
                    end
                    default: begin
                        state <= ST_STOP;
                        phase <= 3'd0;
                        interval_count <= 2'd0;
                    end
                endcase
            end
        end
    endtask

    task automatic sample_read_bit;
        begin
            rx_byte[bit_index] <= sda_i;
            if (bit_index == 3'd0) begin
                if (reading_low_byte)
                    read_data[7:0] <= {rx_byte[7:1], sda_i};
                else
                    read_data[15:8] <= {rx_byte[7:1], sda_i};
            end
        end
    endtask

    task automatic finish_read_bit;
        begin
            scl_drive_low <= 1'b1;
            low_cycle_count <= {HIGH_WIDTH{1'b0}};
            if (bit_index == 3'd0) begin
                state <= ST_MACK;
                phase <= 3'd0;
            end else begin
                bit_index <= bit_index - 1'b1;
                phase <= 3'd0;
            end
        end
    endtask

    task automatic finish_master_ack;
        begin
            scl_drive_low <= 1'b1;
            low_cycle_count <= {HIGH_WIDTH{1'b0}};
            if (reading_low_byte) begin
                state <= ST_STOP;
                phase <= 3'd0;
                interval_count <= 2'd0;
            end else begin
                reading_low_byte <= 1'b1;
                rx_byte <= 8'd0;
                bit_index <= 3'd7;
                state <= ST_READ;
                phase <= 3'd0;
            end
        end
    endtask

    always @(posedge clk or negedge resetn) begin
        if (!resetn) begin
            quantum_count <= {QUANTUM_WIDTH{1'b0}};
            quantum_tick <= 1'b0;
        end else if (quantum_count == QUANTUM_DIV - 1) begin
            quantum_count <= {QUANTUM_WIDTH{1'b0}};
            quantum_tick <= 1'b1;
        end else begin
            quantum_count <= quantum_count + 1'b1;
            quantum_tick <= 1'b0;
        end
    end

    always @(posedge clk or negedge resetn) begin
        if (!resetn) begin
            start_d <= 1'b0;
            start_pending <= 1'b0;
        end else begin
            start_d <= start;
            if (start && !start_d)
                start_pending <= 1'b1;
            if (quantum_tick && state == ST_IDLE && start_pending)
                start_pending <= 1'b0;
        end
    end

    always @(posedge clk or negedge resetn) begin
        if (!resetn) begin
            state                    <= ST_IDLE;
            phase                    <= 3'd0;
            bit_index                <= 3'd7;
            transaction_step         <= 3'd0;
            tx_byte                  <= 8'd0;
            rx_byte                  <= 8'd0;
            latched_read             <= 1'b1;
            latched_device_address   <= 7'h48;
            latched_register_address <= 8'h00;
            latched_write_data       <= 16'd0;
            reading_low_byte         <= 1'b0;
            ack_received             <= 1'b0;
            stretch_count            <= {TIMEOUT_WIDTH{1'b0}};
            high_cycle_count         <= {HIGH_WIDTH{1'b0}};
            low_cycle_count          <= {HIGH_WIDTH{1'b0}};
            interval_count           <= 2'd0;
            scl_drive_low            <= 1'b0;
            sda_drive_low            <= 1'b0;
            busy                     <= 1'b0;
            done                     <= 1'b0;
            ack_error                <= 1'b0;
            timeout_error            <= 1'b0;
            read_data                <= 16'd0;
        end else begin
            done <= 1'b0;

            if (state == ST_START) begin
                case (phase)
                    3'd0: begin
                        scl_drive_low <= 1'b0;
                        sda_drive_low <= 1'b0;
                        high_cycle_count <= {HIGH_WIDTH{1'b0}};
                        stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                        phase <= 3'd1;
                    end
                    3'd1: begin
                        if (scl_i && sda_i) begin
                            stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                            if (high_cycle_count >= HIGH_CYCLES - 1) begin
                                sda_drive_low <= 1'b1;
                                high_cycle_count <= {HIGH_WIDTH{1'b0}};
                                phase <= 3'd2;
                            end else begin
                                high_cycle_count <= high_cycle_count + 1'b1;
                            end
                        end else begin
                            high_cycle_count <= {HIGH_WIDTH{1'b0}};
                            if (stretch_count >= TIMEOUT_CYCLES - 1) begin
                                fail_timeout();
                            end else begin
                                stretch_count <= stretch_count + 1'b1;
                            end
                        end
                    end
                    3'd2: begin
                        if (high_cycle_count >= HIGH_CYCLES - 1) begin
                            scl_drive_low <= 1'b1;
                            low_cycle_count <= {HIGH_WIDTH{1'b0}};
                            high_cycle_count <= {HIGH_WIDTH{1'b0}};
                            state <= ST_SEND;
                            phase <= 3'd0;
                        end else begin
                            high_cycle_count <= high_cycle_count + 1'b1;
                        end
                    end
                    default: begin
                        fail_timeout();
                    end
                endcase
            end else if ((state == ST_SEND || state == ST_ACK ||
                          state == ST_RESTART || state == ST_READ ||
                          state == ST_MACK || state == ST_STOP) &&
                         (phase == 3'd0 || phase == 3'd1)) begin
                // Low-period timing is referenced to the actual resolved-pin
                // falling event (the action that entered this state), not to
                // the free-running quarter-period scheduler.
                if (phase == 3'd0) begin
                    if (low_cycle_count >= DATA_HOLD_CYCLES - 1) begin
                        case (state)
                            ST_SEND: sda_drive_low <= ~tx_byte[bit_index];
                            ST_ACK: sda_drive_low <= 1'b0;
                            ST_RESTART: sda_drive_low <= 1'b0;
                            ST_READ: sda_drive_low <= 1'b0;
                            ST_MACK: sda_drive_low <= ~reading_low_byte;
                            ST_STOP: sda_drive_low <= 1'b1;
                            default: begin end
                        endcase
                        phase <= 3'd1;
                    end else begin
                        low_cycle_count <= low_cycle_count + 1'b1;
                    end
                end else if (low_cycle_count >= LOW_CYCLES - 1) begin
                    scl_drive_low <= 1'b0;
                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                    high_cycle_count <= {HIGH_WIDTH{1'b0}};
                    if (state == ST_STOP)
                        phase <= 3'd3;
                    else
                        phase <= 3'd2;
                end else begin
                    low_cycle_count <= low_cycle_count + 1'b1;
                end
            end else if ((state == ST_SEND || state == ST_ACK || state == ST_RESTART ||
                 state == ST_READ || state == ST_MACK) &&
                (phase == 3'd2 || phase == 3'd3)) begin
                // Wait for the synchronized, resolved SCL input. Start the
                // full high-period timer only after SCL is actually observed
                // high, so pull-up rise and input synchronizer delay cannot
                // shorten the pin-level high interval.
                if (phase == 3'd2) begin
                    if (scl_i) begin
                        stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                        high_cycle_count <= {HIGH_WIDTH{1'b0}};
                        phase <= 3'd3;
                    end else if (stretch_count >= TIMEOUT_CYCLES - 1) begin
                        fail_timeout();
                    end else begin
                        stretch_count <= stretch_count + 1'b1;
                    end
                end else if (!scl_i) begin
                    // A line that falls during its high phase must meet the
                    // same bounded recovery rule as a line that never rises.
                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                    high_cycle_count <= {HIGH_WIDTH{1'b0}};
                    phase <= 3'd2;
                end else begin
                    if (high_cycle_count == HIGH_HALF_CYCLES - 1) begin
                        case (state)
                            ST_ACK: begin
                                ack_received <= !sda_i;
                                if (sda_i)
                                    ack_error <= 1'b1;
                            end
                            ST_READ: sample_read_bit();
                            default: begin end
                        endcase
                    end

                    if (high_cycle_count >= HIGH_CYCLES - 1) begin
                        high_cycle_count <= {HIGH_WIDTH{1'b0}};
                        stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                        case (state)
                            ST_SEND: finish_send_bit();
                            ST_ACK: finish_ack_pulse();
                            ST_READ: finish_read_bit();
                            ST_MACK: finish_master_ack();
                            ST_RESTART: begin
                                sda_drive_low <= 1'b1;
                                interval_count <= 2'd0;
                                phase <= 3'd4;
                            end
                            default: begin
                                state <= ST_ABORT;
                                phase <= 3'd0;
                            end
                        endcase
                    end else begin
                        high_cycle_count <= high_cycle_count + 1'b1;
                    end
                end
            end else if (state == ST_STOP && phase == 3'd3) begin
                if (scl_i) begin
                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                    high_cycle_count <= {HIGH_WIDTH{1'b0}};
                    phase <= 3'd5;
                end else if (stretch_count >= TIMEOUT_CYCLES - 1) begin
                    fail_timeout();
                end else begin
                    stretch_count <= stretch_count + 1'b1;
                end
            end else if (state == ST_STOP && phase == 3'd5) begin
                if (!scl_i) begin
                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                    high_cycle_count <= {HIGH_WIDTH{1'b0}};
                    phase <= 3'd3;
                end else if (high_cycle_count >= HIGH_CYCLES - 1) begin
                    sda_drive_low <= 1'b0;
                    high_cycle_count <= {HIGH_WIDTH{1'b0}};
                    interval_count <= 2'd0;
                    phase <= 3'd4;
                end else begin
                    high_cycle_count <= high_cycle_count + 1'b1;
                end
            end else if (state == ST_RESTART && phase == 3'd4) begin
                if (high_cycle_count >= HIGH_CYCLES - 1) begin
                    scl_drive_low <= 1'b1;
                    low_cycle_count <= {HIGH_WIDTH{1'b0}};
                    high_cycle_count <= {HIGH_WIDTH{1'b0}};
                    tx_byte <= {latched_device_address, 1'b1};
                    bit_index <= 3'd7;
                    state <= ST_SEND;
                    phase <= 3'd0;
                end else begin
                    high_cycle_count <= high_cycle_count + 1'b1;
                end
            end else if (state == ST_STOP && phase == 3'd4) begin
                if (high_cycle_count >= HIGH_CYCLES - 1) begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    state <= ST_IDLE;
                    phase <= 3'd0;
                    interval_count <= 2'd0;
                    high_cycle_count <= {HIGH_WIDTH{1'b0}};
                end else begin
                    high_cycle_count <= high_cycle_count + 1'b1;
                end
            end else if (quantum_tick) begin
                case (state)
                    ST_IDLE: begin
                        scl_drive_low <= 1'b0;
                        sda_drive_low <= 1'b0;
                        phase <= 3'd0;
                        stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                        if (start_pending) begin
                            latched_read             <= read_not_write;
                            latched_device_address   <= device_address;
                            latched_register_address <= register_address;
                            latched_write_data       <= write_data;
                            tx_byte                  <= {device_address, 1'b0};
                            bit_index                <= 3'd7;
                            busy                     <= 1'b1;
                            ack_error                <= 1'b0;
                            timeout_error            <= 1'b0;
                            read_data                <= 16'd0;
                            transaction_step         <= 3'd0;
                            interval_count           <= 2'd0;
                            state                    <= ST_START;
                        end
                    end

                    // Wait for a released bus, require at least 5 us bus-free,
                    // then generate SDA falling while SCL is high. Keep START
                    // asserted for 5 us before lowering SCL.
                    ST_START: begin
                        case (phase)
                            3'd0: begin
                                scl_drive_low <= 1'b0;
                                sda_drive_low <= 1'b0;
                                interval_count <= 2'd0;
                                stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                phase <= 3'd1;
                            end
                            3'd1: begin
                                if (scl_i && sda_i) begin
                                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                    if (interval_count == 2'd1) begin
                                        sda_drive_low <= 1'b1;
                                        interval_count <= 2'd0;
                                        phase <= 3'd2;
                                    end else begin
                                        interval_count <= interval_count + 1'b1;
                                    end
                                end else begin
                                    interval_count <= 2'd0;
                                    if (stretch_count == TIMEOUT_TICKS - 1) begin
                                        timeout_error <= 1'b1;
                                        state <= ST_ABORT;
                                    end else begin
                                        stretch_count <= stretch_count + 1'b1;
                                    end
                                end
                            end
                            default: begin
                                if (interval_count == 2'd1) begin
                                    scl_drive_low <= 1'b1;
                                    interval_count <= 2'd0;
                                    phase <= 3'd0;
                                    state <= ST_SEND;
                                end else begin
                                    interval_count <= interval_count + 1'b1;
                                end
                            end
                        endcase
                    end

                    // Each data bit changes 2.5 us after SCL falls, then SCL
                    // is released 2.5 us later. Once the resolved line is high,
                    // one quantum later the receiver samples and the following
                    // quantum ends the 5 us high interval.
                    ST_SEND: begin
                        case (phase)
                            3'd0: begin
                                sda_drive_low <= ~tx_byte[bit_index];
                                phase <= 3'd1;
                            end
                            3'd1: begin
                                scl_drive_low <= 1'b0;
                                stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                phase <= 3'd2;
                            end
                            3'd2: begin
                                if (scl_i) begin
                                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                    phase <= 3'd3;
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                end
                            end
                            default: begin
                                if (scl_i) begin
                                    scl_drive_low <= 1'b1;
                                    if (bit_index == 3'd0) begin
                                        phase <= 3'd0;
                                        state <= ST_ACK;
                                    end else begin
                                        bit_index <= bit_index - 1'b1;
                                        phase <= 3'd0;
                                    end
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                    phase <= 3'd2;
                                end
                            end
                        endcase
                    end

                    // Release SDA 2.5 us after the preceding falling edge,
                    // raise SCL 2.5 us later, sample the ACK after 2.5 us high,
                    // and lower SCL after the remaining 2.5 us.
                    ST_ACK: begin
                        case (phase)
                            3'd0: begin
                                sda_drive_low <= 1'b0;
                                phase <= 3'd1;
                            end
                            3'd1: begin
                                scl_drive_low <= 1'b0;
                                stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                phase <= 3'd2;
                            end
                            3'd2: begin
                                if (scl_i) begin
                                    ack_received <= !sda_i;
                                    if (sda_i)
                                        ack_error <= 1'b1;
                                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                    phase <= 3'd3;
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                end
                            end
                            default: begin
                                if (scl_i) begin
                                    scl_drive_low <= 1'b1;
                                    if (!ack_received) begin
                                        state <= ST_STOP;
                                        phase <= 3'd0;
                                        interval_count <= 2'd0;
                                    end else begin
                                        case (transaction_step)
                                            3'd0: begin
                                                tx_byte <= latched_register_address;
                                                transaction_step <= 3'd1;
                                                bit_index <= 3'd7;
                                                state <= ST_SEND;
                                                phase <= 3'd0;
                                            end
                                            3'd1: begin
                                                transaction_step <= 3'd2;
                                                bit_index <= 3'd7;
                                                if (latched_read) begin
                                                    state <= ST_RESTART;
                                                    phase <= 3'd0;
                                                    interval_count <= 2'd0;
                                                end else begin
                                                    tx_byte <= latched_write_data[15:8];
                                                    state <= ST_SEND;
                                                    phase <= 3'd0;
                                                end
                                            end
                                            3'd2: begin
                                                if (latched_read) begin
                                                    reading_low_byte <= 1'b0;
                                                    rx_byte <= 8'd0;
                                                    bit_index <= 3'd7;
                                                    state <= ST_READ;
                                                    phase <= 3'd0;
                                                end else begin
                                                    tx_byte <= latched_write_data[7:0];
                                                    bit_index <= 3'd7;
                                                    transaction_step <= 3'd3;
                                                    state <= ST_SEND;
                                                    phase <= 3'd0;
                                                end
                                            end
                                            default: begin
                                                state <= ST_STOP;
                                                phase <= 3'd0;
                                                interval_count <= 2'd0;
                                            end
                                        endcase
                                    end
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                    phase <= 3'd2;
                                end
                            end
                        endcase
                    end

                    // Repeated START: release SDA while SCL is low, raise SCL,
                    // wait 5 us after the resolved rise, then hold START for
                    // another 5 us before transmitting the read address.
                    ST_RESTART: begin
                        case (phase)
                            3'd0: begin
                                sda_drive_low <= 1'b0;
                                phase <= 3'd1;
                            end
                            3'd1: begin
                                scl_drive_low <= 1'b0;
                                stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                interval_count <= 2'd0;
                                phase <= 3'd2;
                            end
                            3'd2: begin
                                if (scl_i) begin
                                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                    if (interval_count == 2'd1) begin
                                        sda_drive_low <= 1'b1;
                                        interval_count <= 2'd0;
                                        phase <= 3'd3;
                                    end else begin
                                        interval_count <= interval_count + 1'b1;
                                    end
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                    interval_count <= 2'd0;
                                end
                            end
                            default: begin
                                if (interval_count == 2'd1) begin
                                    scl_drive_low <= 1'b1;
                                    tx_byte <= {latched_device_address, 1'b1};
                                    bit_index <= 3'd7;
                                    interval_count <= 2'd0;
                                    state <= ST_SEND;
                                    phase <= 3'd0;
                                end else begin
                                    interval_count <= interval_count + 1'b1;
                                end
                            end
                        endcase
                    end

                    // Sample each incoming bit 2.5 us after observed SCL rise;
                    // hold SCL high for 5 us total before lowering it.
                    ST_READ: begin
                        case (phase)
                            3'd0: begin
                                sda_drive_low <= 1'b0;
                                phase <= 3'd1;
                            end
                            3'd1: begin
                                scl_drive_low <= 1'b0;
                                stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                phase <= 3'd2;
                            end
                            3'd2: begin
                                if (scl_i) begin
                                    rx_byte[bit_index] <= sda_i;
                                    if (bit_index == 3'd0) begin
                                        if (reading_low_byte)
                                            read_data[7:0] <= {rx_byte[7:1], sda_i};
                                        else
                                            read_data[15:8] <= {rx_byte[7:1], sda_i};
                                    end
                                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                    phase <= 3'd3;
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                end
                            end
                            default: begin
                                if (scl_i) begin
                                    scl_drive_low <= 1'b1;
                                    if (bit_index == 3'd0) begin
                                        state <= ST_MACK;
                                        phase <= 3'd0;
                                    end else begin
                                        bit_index <= bit_index - 1'b1;
                                        phase <= 3'd0;
                                    end
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                    phase <= 3'd2;
                                end
                            end
                        endcase
                    end

                    ST_MACK: begin
                        case (phase)
                            3'd0: begin
                                // ACK the upper byte; NACK the lower byte.
                                sda_drive_low <= ~reading_low_byte;
                                phase <= 3'd1;
                            end
                            3'd1: begin
                                scl_drive_low <= 1'b0;
                                stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                phase <= 3'd2;
                            end
                            3'd2: begin
                                if (scl_i) begin
                                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                    phase <= 3'd3;
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                end
                            end
                            default: begin
                                if (scl_i) begin
                                    scl_drive_low <= 1'b1;
                                    if (reading_low_byte) begin
                                        state <= ST_STOP;
                                        phase <= 3'd0;
                                        interval_count <= 2'd0;
                                    end else begin
                                        reading_low_byte <= 1'b1;
                                        rx_byte <= 8'd0;
                                        bit_index <= 3'd7;
                                        state <= ST_READ;
                                        phase <= 3'd0;
                                    end
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                    phase <= 3'd2;
                                end
                            end
                        endcase
                    end

                    // STOP: assert SDA while SCL is low, release SCL, wait at
                    // least 5 us after its observed rise, release SDA, then
                    // preserve a further 5 us bus-free interval.
                    ST_STOP: begin
                        case (phase)
                            3'd0: begin
                                scl_drive_low <= 1'b1;
                                stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                phase <= 3'd1;
                            end
                            3'd1: begin
                                sda_drive_low <= 1'b1;
                                phase <= 3'd2;
                            end
                            3'd2: begin
                                scl_drive_low <= 1'b0;
                                stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                interval_count <= 2'd0;
                                phase <= 3'd3;
                            end
                            3'd3: begin
                                if (scl_i) begin
                                    stretch_count <= {TIMEOUT_WIDTH{1'b0}};
                                    if (interval_count == 2'd1) begin
                                        sda_drive_low <= 1'b0;
                                        interval_count <= 2'd0;
                                        phase <= 3'd4;
                                    end else begin
                                        interval_count <= interval_count + 1'b1;
                                    end
                                end else if (stretch_count == TIMEOUT_TICKS - 1) begin
                                    timeout_error <= 1'b1;
                                    state <= ST_ABORT;
                                end else begin
                                    stretch_count <= stretch_count + 1'b1;
                                    interval_count <= 2'd0;
                                end
                            end
                            3'd4: begin
                                if (interval_count == 2'd1) begin
                                    busy <= 1'b0;
                                    done <= 1'b1;
                                    state <= ST_IDLE;
                                    phase <= 3'd0;
                                    interval_count <= 2'd0;
                                end else begin
                                    interval_count <= interval_count + 1'b1;
                                end
                            end
                            default: begin
                                state <= ST_ABORT;
                            end
                        endcase
                    end

                    // A bounded bus failure completes without attempting to
                    // manufacture a STOP on a line that may be held low.
                    ST_ABORT: begin
                        scl_drive_low <= 1'b0;
                        sda_drive_low <= 1'b0;
                        busy <= 1'b0;
                        done <= 1'b1;
                        phase <= 3'd0;
                        state <= ST_IDLE;
                    end

                    default: begin
                        state <= ST_ABORT;
                        phase <= 3'd0;
                    end
                endcase
            end
        end
    end
endmodule
