# 통신 규격

기준은 CAN DO 시스템 설계서 **v1.5 부록 C0**입니다. C0가 바꾼 항목에는 C0를 적용하고, 세부 바이트 배치와 시험·로그 규칙은 아래 관련 절을 참조합니다.

| 링크 | 기본 구성 | 참고 절 |
| --- | --- | --- |
| Qt ↔ backend | TCP 5000, `can-do/1.1`, UTF-8 JSON 객체 1개에 LF 1개, LF 포함 최대 16,384바이트 | C0.2, C7 |
| backend ↔ 모터 STM32 | Classical CAN, 125 kbit/s, 표준 11-bit ID, 정의된 프레임 DLC 8, 다중 바이트 값 Little-endian, 프로젝트 CAN 규격 2.1 | C0.3, C2~C6 |
| FPGA → backend | 내장 USB-UART, 115200 8N1, 고정 22바이트 프레임, CRC-16/CCITT-FALSE | C0.4, C8~C10 |

### Qt와 backend

서버 `hello`가 현재 `stream_id`와 `hello.data.protocol` 값 `"2.1"`을 보냅니다. Qt는 현재 `stream_id`로 `control_heartbeat`와 `control_request`를 보냅니다. 요청은 `request_id`로 식별하며 서버는 `control_result`로 처리 결과를 반환합니다. Qt는 처리 결과와 실제 MCU 상태 스냅샷을 각각 표시합니다. 응답 유실 시 구동 명령을 자동 재시도하지 않습니다.

### backend와 모터 STM32

backend가 `OP_HELLO`(`0x701`), `OP_PERMIT`(`0x080`), `COMMAND`(`0x100`)를 전송합니다. 양측 HELLO 버전은 `2.1`이며 `2.0`에는 운전 허가를 부여하지 않습니다. `OP_PERMIT`의 b0은 제어 준비, b2는 출력 억제 요청이며 기본 구성의 b1·b3·b4는 0입니다. NC2는 `MOTOR_HELLO.capabilities` b4와 `MOTOR_STATE.status_flags` b7을 통해 보고합니다.

초기 제안값은 Qt heartbeat 100ms 주기·300ms 만료, CAN 허가 50ms 주기·모터 수신 기준 200ms 만료입니다. Qt 연결 종료를 감지하거나 heartbeat가 만료되면 backend는 신규 운전 요청을 차단하고 허가를 철회합니다. 재연결과 새 heartbeat는 운전 요청을 대신하지 않습니다.

### FPGA 시험

`TEST_READY`는 시험 시작 준비 상태를 나타냅니다. 시험 실행 판정은 별도로 CAN의 `TEST_ACTIVE`와 FPGA의 `BASELINE_START`부터 `END`까지의 증거를 연결합니다. 기본 시험은 `NORMAL`, `NO_EDGE`, `GAP_500`입니다. 시간 판정은 FPGA가 같은 시계에서 관측한 주입과 진단 응답을 사용합니다.

### 통합 전에 확정할 항목

- STOP 이후 받은 `STOPPED` 또는 `FAULT`가 해당 억제 요청 처리 이후에 생성됐는지 확인하는 기준
- 실측형 FPGA 프로파일의 식별 정보를 시험 `run_id`에 연결하는 방법과 `GAP_500` 복귀 이후 발행된 CAN 근거를 구분하는 기준
- 비상정지 NC2 입력의 핀·전기 조건 및 실물 USB-CAN 송신·종단 확인

[공통 입력 예제](../tests/fixtures/)에서 위 기본 버전의 바이트와 메시지를 확인할 수 있습니다.
