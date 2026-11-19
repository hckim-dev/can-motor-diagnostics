# FPGA

Basys3의 A/B 시험 파형, `TEST_READY`·`DIAG_FAULT` 관측, USB-UART 이벤트 전송을 구현하는 작업 영역입니다.

첫 작업은 RTL 테스트벤치에서 NORMAL, NO_EDGE, GAP_500의 파형, 이벤트 순서, 시간값과 CRC를 확인하는 것입니다. [FPGA 통신 규격](../docs/protocol.md)과 [UART 예제](../tests/fixtures/fpga-uart-1.0.hex)를 참조합니다.

프로젝트를 추가할 때 Vivado 버전, 프로젝트 시작점, 시뮬레이션 방법을 이 문서에 기록합니다. 빌드에 필요한 RTL, XDC, 프로젝트 설정과 테스트벤치를 추적합니다.
