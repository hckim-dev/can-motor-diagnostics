# 공통 입력 예제

다음 예제는 코덱 및 목업에서 사용합니다. 식별자와 수치는 합성 예시입니다.

| 파일 | 입력과 기대값 |
| --- | --- |
| [can-2.1.jsonl](can-2.1.jsonl) | 표준 CAN 프레임의 ID와 DLC, 공백으로 구분한 payload 바이트. Jetson·모터 HELLO는 2.1이고, 허가·억제 프레임은 같은 epoch에서 연속된 alive_seq를 사용합니다. |
| [tcp-qt-to-backend.jsonl](tcp-qt-to-backend.jsonl) | Qt가 보내는 heartbeat와 시험 준비 요청. 요청 인자는 0입니다. |
| [tcp-backend-to-qt.jsonl](tcp-backend-to-qt.jsonl) | backend가 보내는 목업 hello·결과·스냅샷. 두 TCP 파일은 같은 stream_id와 request_id를 사용합니다. |
| [fpga-uart-1.0.hex](fpga-uart-1.0.hex) | v1.5 C8의 22바이트 INJECT 벡터. 시나리오 2, tick 1,000,000µs, CRC 0xB09E를 기대합니다. |

TCP 파일은 LF로 구분한 UTF-8 JSON 객체입니다. UART 파일은 디코더 입력 전에 hex를 바이트로 변환합니다. 예제는 필드와 경계를 검증하기 위한 입력이며 전체 운전·시험의 계측 기록은 아닙니다.
