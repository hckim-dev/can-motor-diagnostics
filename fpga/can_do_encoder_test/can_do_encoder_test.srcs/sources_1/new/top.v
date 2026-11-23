`timescale 1ns / 1ps

module top(
    input clk,
    input reset,
    input [1:0] sw,
    input uart_rx,
    output uart_tx,
    output enc_a,
    output enc_b,
    output [3:0] an,
    output [7:0] seg,
    output test_ready,
    output diag_fault
    );

    wire tick;
    wire [13:0] in_data;
    wire [1:0] motor_direction;    // in1 in2 : 10(정방향(f))  01: 역방향(b)
    wire error_active;

    timebase u_timebase(
        .clk(clk),
        .reset(reset),
        .tick(tick)
    );

    input_conditoner u_input_conditoner(
        .clk(clk),
        .reset(reset),
        .enc_a(enc_a),
        .enc_b(enc_b),
        .tick(tick),
        .in_data(in_data),
        .motor_direction(motor_direction), 
        .error_active(error_active),
        .test_ready(test_ready),
        .diag_fault(diag_fault)
    );

    scenario_ctrl u_scenario_ctrl(
        .clk(clk),
        .reset(reset),
        .tick(tick),
        .in_data(in_data),
        .motor_direction(motor_direction), 
        .error_active(error_active)
    );

    encoder_ab u_encoder_ab(
        .clk(clk),
        .reset(reset),
        .enc_a(enc_a),
        .enc_b(enc_b),
        .tick(tick),
        .in_data(in_data),
        .motor_direction(motor_direction), 
        .error_active(error_active)
    );

    diag_capture u_diag_capture(
        .clk(clk),
        .reset(reset),
        .tick(tick),
        .in_data(in_data),
        .motor_direction(motor_direction), 
        .error_active(error_active),
        .diag_fault(diag_fault)
    );
    
    event_store u_event_store(
        .clk(clk),
        .reset(reset),
        .tick(tick),
        .in_data(in_data),
        .motor_direction(motor_direction), 
        .error_active(error_active)
    );

    event_fifo u_event_fifo(
        .clk(clk),
        .reset(reset),
        .tick(tick),
        .in_data(in_data),
        .motor_direction(motor_direction), 
        .error_active(error_active)
    );

    frame_encoder u_frame_encoder(
        .clk(clk),
        .reset(reset),
        .tick(tick),
        .in_data(in_data),
        .motor_direction(motor_direction), 
        .error_active(error_active)
    );

    uart_tx u_uart_tx(
        .clk(clk),
        .reset(reset),
        .tick(tick),
        .in_data(in_data),
        .motor_direction(motor_direction), 
        .error_active(error_active)
    );

    fnd_controller u_fnd_controller(
        .clk(clk),
        .reset(reset),
        .tick(tick),
        .in_data(in_data),
        .motor_direction(motor_direction), 
        .error_active(error_active),
        .an(an),
        .seg(seg)
);
endmodule
