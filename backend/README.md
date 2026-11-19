# Backend

Jetson Orin Nano에서 실행하는 CAN DO 제어·진단 Python 서비스 작업 영역입니다. Qt TCP 연결, CAN 명령·운전 허가 발행, CAN·FPGA UART 수집, 시험 판정, 로그·리플레이를 담당합니다.

읽기 전용 TCP 목업은 Jetson Ubuntu의 Python 3.10.12에서 검증했습니다. 서버와 Python 검증 클라이언트 모두 표준 라이브러리만 사용합니다. 저장소 루트의 두 터미널에서 각각 실행합니다.

```sh
# Jetson 터미널 1
python3 backend/tcp_mock.py

# Jetson 터미널 2
python3 tests/check_tcp_mock.py
```

서버는 기본값으로 `127.0.0.1:5000`에서 한 연결을 받습니다. `can-do/1.1`의 `hello`와 초기 `TEST / STOPPED` 스냅샷을 먼저 보내고, 같은 모의 상태를 10Hz로 전송합니다. 클라이언트가 데이터를 보내면 연결을 종료합니다. Ctrl+C로 서버를 종료할 수 있습니다.

다른 PC에서 검증하려면 Jetson 서버에 `--host <Jetson LAN IP>`를 지정하고, PC에서 `python tests/check_tcp_mock.py --host <Jetson LAN IP>`를 실행합니다. 포트를 변경할 경우 양쪽의 `--port`를 맞춥니다. 검증 클라이언트는 공통 fixture와 수신 내용을 대조하고 전송 주기와 재접속 시 새 `stream_id`를 확인합니다.

실물 CAN과 FPGA UART는 이후 단계에서 연결합니다. [통신 규격](../docs/protocol.md)과 [공통 입력 예제](../tests/fixtures/)를 참조합니다.
