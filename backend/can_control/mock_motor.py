"""메모리 안에서만 CAN 명령에 응답하는 시험용 모터 상태를 만든다."""

from collections import Counter, deque

from ..can_rx.codec import (
    DIAG_FAULT_OUTPUT,
    ENCODER_CALIBRATED,
    OP_LINK_VALID,
    OPERATING_CONFIG_VALID,
    STOP_STATES,
    SUPPLY_PRESENT,
    SUPPLY_VALID,
    TEST_READY_OUTPUT,
    VERSION,
    WORD_BYTES,
    WORD_SHORT_BYTES,
    CanId,
    Command,
    Frame,
    InvalidFrame,
    Mode,
    OperatorHello,
    Permit,
    State,
    decode_frame,
)
from ..can_rx.samples import NS_PER_MS, SEQUENCE_HALF, forward_delta
from ..can_rx.state import (
    CONTROLS_READY,
    FAULT_SEQ_MASK,
    NC2_CAPABILITY,
    PERMIT_LIFETIME,
    STOP_REQUESTED,
)
from .wire import MAX_CMD_SEQ, Opcode

STATE_PERIOD_NS = 100 * NS_PER_MS
INITIAL_EPOCH = 0x00010001
SESSION_COUNTER_MASK = 0xFFFF
OP_LINK_LOSS_FAULT = 1 << 1
INTERNAL_FAULT = 1 << 8
MOTOR_CAPABILITIES = 0x0F | NC2_CAPABILITY


class MockMotor:
    def __init__(self, mode=Mode.TEST):
        self.mode = mode
        self.state = State.STOPPED
        self.epoch = INITIAL_EPOCH
        self.epoch_exhausted = False
        self.errors = Counter()
        self.operator_boot = 0
        self.permit_until_ns = None
        self.alive_seq = None
        self.last_cmd_seq = 0
        self.command_results: dict[int, tuple[Command, Frame]] = {}
        self.fault_mask = 0
        self.fault_event_seq = 0
        self.next_state_ns = 0

    def state_frames(self):
        flags = SUPPLY_VALID | ENCODER_CALIBRATED | OPERATING_CONFIG_VALID
        if self.mode == Mode.REAL:
            flags |= SUPPLY_PRESENT
        if self.permit_until_ns is not None:
            flags |= OP_LINK_VALID
        if self.fault_mask:
            flags |= DIAG_FAULT_OUTPUT
        if self.state in (State.TEST_ARMED, State.TEST_ACTIVE):
            flags |= TEST_READY_OUTPUT
        return (
            Frame(
                CanId.MOTOR_HELLO,
                WORD_BYTES.pack(self.epoch, *VERSION, MOTOR_CAPABILITIES, 0),
            ),
            Frame(
                CanId.MOTOR_STATE,
                WORD_BYTES.pack(
                    self.epoch,
                    self.state,
                    self.mode,
                    flags,
                    self.last_cmd_seq,
                ),
            ),
            Frame(
                CanId.MOTOR_FAULT,
                WORD_SHORT_BYTES.pack(
                    self.epoch,
                    self.fault_mask,
                    self.fault_event_seq,
                    self.mode,
                ),
            ),
        )

    def latch_fault(self, fault):
        if not self.fault_mask & fault:
            self.fault_mask |= fault
            self.fault_event_seq = (self.fault_event_seq + 1) & FAULT_SEQ_MASK

    def stop_and_renew(self):
        self.permit_until_ns = None
        if self.epoch & SESSION_COUNTER_MASK == SESSION_COUNTER_MASK:
            self.epoch_exhausted = True
            self.latch_fault(INTERNAL_FAULT)
        else:
            self.epoch += 1
            self.alive_seq = None
            self.last_cmd_seq = 0
            self.command_results.clear()
        self.state = State.FAULT if self.fault_mask else State.STOPPED

    def expire_permit(self, now_ns):
        if self.permit_until_ns is not None and now_ns >= self.permit_until_ns:
            self.latch_fault(OP_LINK_LOSS_FAULT)
            self.stop_and_renew()
            self.next_state_ns = now_ns

    def tick(self, now_ns):
        self.expire_permit(now_ns)
        if now_ns >= self.next_state_ns:
            self.next_state_ns = now_ns + STATE_PERIOD_NS
            return self.state_frames()
        return ()

    def receive(self, frame, now_ns):
        self.expire_permit(now_ns)
        try:
            message = decode_frame(frame)
        except InvalidFrame:
            self.errors[frame.can_id] += 1
            return ()

        if isinstance(message, OperatorHello):
            if message.version == VERSION and message.boot_id > self.operator_boot:
                if self.operator_boot:
                    self.stop_and_renew()
                    self.next_state_ns = now_ns
                self.operator_boot = message.boot_id
        elif isinstance(message, Permit):
            if (
                message.epoch != self.epoch
                or not self.operator_boot
                or self.epoch_exhausted
            ):
                return ()
            if self.alive_seq is not None and not (
                1 <= forward_delta(message.alive_seq, self.alive_seq) < SEQUENCE_HALF
            ):
                return ()
            self.alive_seq = message.alive_seq
            if message.flags & STOP_REQUESTED or (
                not message.flags & CONTROLS_READY and self.permit_until_ns is not None
            ):
                self.stop_and_renew()
                self.next_state_ns = now_ns
            elif message.flags & CONTROLS_READY:
                self.permit_until_ns = now_ns + PERMIT_LIFETIME
        elif isinstance(message, Command):
            return self.apply_command(message, now_ns)
        return ()

    def apply_command(self, message, now_ns):
        cached = self.command_results.get(message.cmd_seq)
        result, detail = 1, 0
        record_result = False
        if message.epoch != self.epoch:
            result, detail = 2, 1
        elif message.opcode == Opcode.STOP:
            # 현재 epoch의 STOP은 순번 오류가 있어도 출력을 억제한다.
            if message.cmd_seq <= self.last_cmd_seq:
                result, detail = 2, 2
            self.stop_and_renew()
            self.next_state_ns = now_ns
        elif self.epoch_exhausted:
            result, detail = 2, 5
        elif cached is not None and cached[0] == message:
            return (cached[1],)
        elif message.cmd_seq <= self.last_cmd_seq:
            result, detail = 2, 2
        else:
            self.last_cmd_seq = message.cmd_seq
            record_result = True
            if self.permit_until_ns is None:
                result, detail = 2, 5
            elif message.opcode == Opcode.RESET and self.state == State.FAULT:
                self.fault_mask = 0
                self.fault_event_seq = (self.fault_event_seq + 1) & FAULT_SEQ_MASK
                self.state = State.STOPPED
            elif self.state != State.STOPPED:
                result, detail = 2, 3
            elif message.opcode == Opcode.START_REAL and self.mode == Mode.REAL:
                self.state = State.RUNNING
            elif message.opcode == Opcode.ARM_TEST and self.mode == Mode.TEST:
                self.state = State.TEST_ARMED
            else:
                result, detail = 2, 3
            self.next_state_ns = now_ns

        reply = Frame(
            CanId.COMMAND_RESULT,
            WORD_BYTES.pack(
                message.epoch,
                message.cmd_seq,
                result,
                detail,
                self.state,
            ),
        )
        if record_result:
            self.command_results[message.cmd_seq] = (message, reply)
            if message.cmd_seq == MAX_CMD_SEQ and self.state in STOP_STATES:
                self.stop_and_renew()
        return (reply,)


class MemoryCan:
    def __init__(self, motor):
        self.motor = motor
        self.outgoing = deque()

    def try_send(self, frame):
        self.outgoing.append(frame)
        return True

    def step(self, controller, now_ns):
        for frame in self.motor.tick(now_ns):
            controller.receive(frame, now_ns)
        controller.poll(now_ns)

        while self.outgoing:
            frame = self.outgoing.popleft()
            # 송신한 조작 프레임도 수신 모듈이 관측하도록 되돌린다.
            if frame.can_id in (CanId.OP_HELLO, CanId.OP_PERMIT):
                controller.receive(frame, now_ns)
            for reply in self.motor.receive(frame, now_ns):
                controller.receive(reply, now_ns)

        for frame in self.motor.tick(now_ns):
            controller.receive(frame, now_ns)
