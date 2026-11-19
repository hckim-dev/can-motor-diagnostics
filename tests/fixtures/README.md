# 공통 입력 예제

다음 예제는 코덱 및 목업에서 사용합니다. 식별자와 수치는 합성 예시입니다.

| 파일 | 입력과 기대값 |
| --- | --- |
| [can-2.1.jsonl](can-2.1.jsonl) | 표준 CAN 프레임의 ID, DLC 및 payload 바이트. 양측 HELLO 2.1과 초기 `STOPPED`, 허가, `ARM_TEST` 명령·결과, `TEST_ARMED`, 허가 철회 요청 예제가 순서대로 들어 있습니다. |
| [can-rx-timed.jsonl](can-rx-timed.jsonl) | `at_ms` 순으로 HELLO·상태·고장·허가와 한 묶음의 측정값을 공급합니다. C3 측정 예제의 59 RPM, 450‰ PWM, 250 mA, 6000 mV 값을 기대합니다. |
| [tcp-qt-to-backend.jsonl](tcp-qt-to-backend.jsonl) | Qt가 보내는 heartbeat와 시험 준비 요청. 요청 인자는 0입니다. |
| [tcp-backend-to-qt.jsonl](tcp-backend-to-qt.jsonl) | backend가 보내는 목업 hello, 초기 `STOPPED` 스냅샷, 시험 준비 결과, `TEST_ARMED` 스냅샷. 두 TCP 파일의 연결과 요청 식별자가 대응하고, 결과의 epoch·cmd_seq는 CAN 예제와 일치합니다. |
| [fpga-uart-1.0.hex](fpga-uart-1.0.hex) | v1.5 C8의 22바이트 INJECT 벡터. 시나리오 2, tick 1,000,000µs, CRC 0xB09E를 기대합니다. |

TCP 파일은 LF로 구분한 UTF-8 JSON 객체입니다. UART 파일은 디코더 입력 전에 hex를 바이트로 변환합니다. 예제는 필드와 경계를 검증하기 위한 입력이며 전체 운전·시험의 계측 기록은 아닙니다.
