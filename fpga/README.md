# FPGA

Basys3의 A/B 시험 파형, `TEST_READY`·`DIAG_FAULT` 관측, USB-UART 이벤트 전송을 구현하는 작업 영역입니다.

첫 작업은 RTL 테스트벤치에서 NORMAL, NO_EDGE, GAP_500의 파형, 이벤트 순서, 시간값과 CRC를 확인하는 것입니다. [FPGA 통신 규격](../docs/protocol.md)과 [UART 예제](../tests/fixtures/fpga-uart-1.0.hex)를 참조합니다.


## 설계 문서

- [FPGA 구현 스펙 v1.4](../docs/FPGA_구현_스펙_v1.4.md) · [PDF](../docs/FPGA_구현_스펙_v1.4.pdf)
- [FPGA 구현 스펙 v1.3](../docs/FPGA_구현_스펙_v1.3.md)
- [FPGA 모듈 구조 및 포트](FPGA_모듈구조.md)
- [FPGA 모듈 연결도](FPGA_모듈_연결도.svg)
- [시스템 설계 v1.5](../CAN_DO_System_Design_v1.5.docx)
- [시험 지그 완성 예상도와 구매 목록](../docs/assets/motor-test-jig-overview-v2.png)

## 인수인계

- 자동 시험 요구사항은 수동 스위치 선택에서 일괄 실행으로 변경됐다. 기본 순서는 NORMAL → NO_EDGE → GAP_500이다. 시나리오 사이에는 이전 회차 마감 확인, STM32 STOP, 필요 시 RESET, 새 ARM_TEST와 TEST_READY 상승이 필요하다. 새 배치의 시작·중단·재시작을 구분하는 신호 계약은 미확정이다. 스펙 v1.4와 연결도의 수동 시작·선택 부분은 이 요구사항을 반영하기 전 설계다.
- [Vivado 프로젝트](can_do_encoder_test/can_do_encoder_test.xpr)는 작업 초안이다. 현재 설정은 Versal 부품과 작업 PC의 외부 XDC를 참조한다. Basys3 목표 부품은 `xc7a35tcpg236-1`이다. 현재 Top의 신호 방향과 하위 모듈 포트를 설계에 맞춰야 하며, 하위 RTL 파일 9개는 빈 자리표시자다. 합성과 보드 시험은 미실행이다.
- 지그 구매처는 디바이스마트와 쿠팡으로 제한한다. 지그 이미지 기준 배송비 포함 기본 예산은 44,980원이며, 멀티미터를 추가하면 49,380원이다. 실제 조립과 출고 일정은 확인 전이다.
