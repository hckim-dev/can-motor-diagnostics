# CAN DO

CAN DO는 6V 모터 제어보드가 엔코더 피드백 유실을 감지하고 보고하는지 검증하는 팀 프로젝트입니다. Qt에서 운전과 시험 준비를 요청하고, FPGA에서 재현한 시험 신호에 대한 STM32의 응답을 Jetson에서 판정합니다.

```text
PC Qt ↔ TCP ↔ Jetson backend ↔ CAN ↔ 모터 STM32
Basys3 → USB-UART → Jetson backend
Basys3 → 시험 A/B → MUX → 모터 STM32
모터 STM32 → TEST_READY / DIAG_FAULT → Basys3
```

## 작업 영역

| 경로 | 담당 영역 |
| --- | --- |
| [qt/](qt/) | PC Qt/C++ 조작 화면, 상태와 요청 결과 표시 |
| [backend/](backend/) | Jetson Orin Nano에서 실행하는 Python 제어·진단 서비스 |
| [firmware/motor-stm32/](firmware/motor-stm32/) | NUCLEO-F103RB 모터 제어·보호·CAN 보고 |
| [fpga/](fpga/) | Basys3 시험 파형, GPIO 응답 시간 측정, UART 전송 |
| [tests/fixtures/](tests/fixtures/) | 부품 간 송수신을 검증하는 공통 입력 예제 |

먼저 [시스템 구조](docs/architecture.md)와 [통신 규격](docs/protocol.md)을 확인하세요. 개발과 통합은 [단계별 합격 기준](docs/integration.md)을 따릅니다. 프레임이나 상태 전이를 바꾸는 PR에서는 관련 문서와 예제를 함께 갱신하고, 송신·수신 담당자가 함께 검토합니다.

현재 저장소에는 작업 골격, 설계 기준, 입력 예제가 있습니다. 각 영역에 구현이 추가되면 해당 README에 도구 버전과 빌드·실행 방법을 기록합니다.
