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

조작 목업은 `python3 backend/tcp_mock.py --control`로 실행합니다. 현재 연결의 heartbeat를 받은 뒤 `ARM_TEST(0)`의 `COMPLETED` 결과와 `TEST_ARMED` 스냅샷을 별도로 전송합니다. heartbeat가 300ms 동안 없으면 준비를 해제하고 `STOPPED`로 돌아갑니다. 다시 준비하려면 유효한 heartbeat와 새 요청이 필요합니다. `START_REAL`, `STOP`, `RESET`은 인자를 검사한 뒤 미지원으로 응답합니다.

같은 연결에서 같은 요청을 반복하면 이전 결과를 반환합니다. 같은 ID의 내용이 바뀌거나 재접속에서 이전 요청 ID를 재사용하면 거부합니다. 요청 기록은 목업 프로세스에 최대 255건을 보관합니다. 한도에 도달하면 기존 기록을 유지하고 새 요청을 거부하며, 목업을 재시작하면 기록이 초기화됩니다.

모의 명령 순번 `cmd_seq`는 성공한 새 요청에만 발급합니다. 목업의 고정 epoch 안에서는 재접속해도 순번이 이어집니다.

`tcp_mock.py`는 TCP 연결, 메시지 경계, 응답 전송을 담당합니다. `tcp_mock_control.py`는 heartbeat 만료, 시험 준비 상태, 중복 요청을 처리합니다.

`python3 -X dev -W error tests/check_tcp_control.py -v`로 별도 서버를 임시 포트에 띄워 정상 조작, 만료, 재접속, 잘못된 입력을 검증할 수 있습니다.
