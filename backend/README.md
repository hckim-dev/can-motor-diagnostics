# Backend

Jetson Orin Nano에서 실행하는 CAN DO 제어·진단 Python 서비스 작업 영역입니다. Qt TCP 연결, CAN 명령·운전 허가 발행, CAN·FPGA UART 수집, 시험 판정, 로그·리플레이를 담당합니다.

첫 작업은 실물 CAN에 연결되지 않는 목업으로 Qt와 메시지 및 연결 상태를 검증하는 것입니다. [통신 규격](../docs/protocol.md)과 [공통 입력 예제](../tests/fixtures/)를 참조합니다.

구현을 추가할 때 Python·의존성 버전, 목업 실행 방법과 Jetson 실물 실행 방법을 이 문서에 기록합니다.
