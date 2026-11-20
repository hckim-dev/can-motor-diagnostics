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

`tcp_stream.py`는 TCP 연결, 메시지 경계, 응답 전송을 담당합니다. `tcp_mock.py`는 이 공통 경로에 목업의 상태와 예제 입력을 연결하고, `tcp_mock_control.py`는 heartbeat 만료, 시험 준비 상태, 중복 요청을 처리합니다.

`python3 -X dev -W error tests/check_tcp_control.py -v`로 별도 서버를 임시 포트에 띄워 정상 조작, 만료, 재접속, 잘못된 입력을 검증할 수 있습니다.

CAN 수신 목업은 `python3 backend/tcp_mock.py --can-fixture tests/fixtures/can-rx-timed.jsonl`로 실행합니다. 예제를 서버 시작 시점부터 한 번 공급하며, 유효한 수신이 없으면 TCP 스냅샷의 모터 상태는 `UNKNOWN`, 측정값은 `null`입니다. 예제 공급이 끝난 뒤에도 스냅샷을 보내며 각 필드는 규격에 따라 만료됩니다. 처음부터 다시 보려면 서버를 재시작합니다.

`can_rx/codec.py`는 CAN 프레임을 디코딩하고, `can_rx/samples.py`는 측정 프레임을 결합합니다. `can_rx/state.py`는 HELLO·epoch·만료를 적용해 TCP 상태를 구성하고, 수신 순번·단조 시각·원본 ID를 부여합니다. `can_rx/fixture.py`는 예제 입력만 공급합니다. 새 부팅이나 연결 복구 시 300ms 동안 측정 결합을 보류합니다. 이 시간 이후의 지연 프레임 혼합을 막으려면 실제 어댑터의 큐·지연 조건을 별도로 확인해야 합니다.

조작 노드의 새 부팅 ID를 관측하면 이후 더 큰 모터 epoch를 알리는 `MOTOR_HELLO`와 그 epoch의 유효한 `OP_PERMIT`가 모두 들어올 때까지 허가를 미확인으로 표시합니다.

`python3 -X dev -W error -m unittest discover -s tests -p 'check_can_rx.py' -v`로 디코딩, 상태, TCP 스냅샷을 검증합니다.

모의 CAN 조작은 저장소 루트에서 `python3 -m backend.tcp_can_mock`로 실행합니다. 기본 모드는 TEST이며, `--motor-mode REAL`은 가상 모터에서만 `START_REAL`을 확인하는 옵션입니다. `--boot-id-file`의 기본 경로는 `.local/can-mock-boot-id`이며, 서버를 시작할 때마다 증가한 부팅 ID를 저장합니다.

`can_control/wire.py`는 송신 바이트를 구성하고, `can_control/controller.py`는 명령·허가·타임아웃을 관리합니다. `can_control/mock_motor.py`는 메모리 버스와 가상 모터를 제공하며, `tcp_can_mock.py`는 TCP 요청과 비동기 CAN 결과를 연결합니다.

제어기는 입력 시각의 만료를 먼저 처리합니다. CAN 프레임을 수신하면 디코딩과 수신 검증을 거친 뒤 epoch와 허가 상태를 재평가하고, 명령 결과를 대기 요청에 연결합니다. CAN 단절 시에는 이전 억제 송신의 확인 기준을 폐기합니다. 재연결 후 새 억제 송신과 정지 관측을 거쳐야 허가가 복구됩니다.

모의 CAN은 허가를 50ms마다 보내고, 조작 heartbeat가 300ms 동안 없으면 대기 명령을 취소하고 출력 억제를 요청합니다. 명령의 송신 대기 50ms와 송신 후 결과 대기 300ms는 별개입니다. 명령 송신은 모의 버스 주기에서 처리합니다. 주기 전에 STOP이나 연결 단절이 들어오면 대기 명령을 취소하고, 버스가 연결된 경우 다음 주기에 출력 억제 프레임을 보냅니다. 모의 환경에서는 억제 송신 후 더 큰 epoch의 최신 `STOPPED` 또는 `FAULT`를 확인해야 새 운전 허가를 내보냅니다. 이 규칙과 실제 어댑터의 프레임 취소 범위는 실물 연결 전 별도로 확인합니다.

`python3 -X dev -W error -m unittest discover -s tests -p 'check_can_control.py' -v`로 모의 CAN과 TCP 조작을 검증합니다.

## FPGA UART 수신

CAN DO 설계서 v1.5 C8의 22바이트 프레임을 수신합니다. `fpga_uart/codec.py`가 CRC와 필드를 검사하고, `stream.py`가 분할 수신 및 CRC 오류 이후의 프레임 경계를 복원합니다. `session.py`는 연결마다 UUID를 발급하고 `(fpga_session, run_seq)`로 시험 회차를 추적합니다. 순번 공백·CRC 오류·FPGA overflow를 관측한 회차는 이후 `STATUS`가 와도 오류 기록을 유지합니다. 역행으로 세션이 모호해지면 다음 명시적 연결 또는 리셋까지 회차 입력을 보류합니다.

```sh
python3 -m backend.fpga_uart --hex tests/fixtures/fpga-uart-1.0.hex
python3 -m backend.fpga_uart path/to/capture.bin
python3 -X dev -W error -m unittest discover -s tests -p 'check_fpga_uart.py' -v
```

검증 CLI는 디코딩 결과·오류·관측된 회차를 JSON 줄로 출력합니다. 공통 예제는 `INJECT` 한 프레임이라 `MISSING_BASELINE`이 함께 표시됩니다. 파일 입력에는 읽은 시점의 단조 시각을 부여하고, 보드 시간은 프레임의 `tick_us`로 유지합니다. 실물 포트 설정과 연결은 7단계에서, CAN과 결합한 시험 판정은 6단계에서 검증합니다.

`STATUS`에서 확인한 FPGA 오류 비트는 현재 UART 세션에 유지합니다. 이후 `STATUS`에서 비트가 내려가도 같은 세션의 새 회차에 오류를 표시합니다. 보드 리셋이나 명시적 재연결로 새 세션을 열면 다시 관측하며, FPGA가 오류를 계속 보고하면 새 세션에도 기록합니다.

## 시험 판정·기록·리플레이

`diagnostics`는 모의 CAN·UART 원본을 입력 순서대로 기존 수신기에 전달하고, `fpga-basic/1.0`의 `NORMAL`, `NO_EDGE`, `GAP_500`을 판정합니다. 유휴 `STATUS`, 새 `BASELINE_START`, CAN의 `TEST_ARMED`와 `TEST_ACTIVE`를 확인합니다. `END` 뒤에는 최대 500ms 동안 지연된 근거를 수집합니다. `GAP_500`은 이 구간에 새로 받은 동일 부팅·TEST 세션의 `MOTOR_STATE`와 `MOTOR_FAULT`를 요구합니다.

```sh
python3 -m backend.diagnostics record tests/fixtures/diagnostic-no-edge.jsonl \
  .local/diagnostic-original --gpio-verified
python3 -m backend.diagnostics replay .local/diagnostic-original \
  .local/diagnostic-replay
python3 -X dev -W error -m unittest discover -s tests -p 'check_diagnostics.py' -v
```

두 출력 경로에는 새 디렉터리를 지정합니다. 공통 예제의 합성 GPIO 검증 전제는 `--gpio-verified`로 적용하며, 이 옵션 없이 실행하면 `INCONCLUSIVE / TIMING_UNAVAILABLE`가 됩니다. CLI는 원본 입력 `inputs.jsonl`, CAN·UART 원본 조회용 `raw_can.jsonl`·`raw_fpga.jsonl`, 판정 및 증거 ID `events.jsonl`, 설정과 입력 해시 `manifest.json`을 저장합니다. `clock` 서비스 입력은 기록이 도달한 시각을 표시하며, 필요한 구간 전에 EOF가 오면 `INCONCLUSIVE`로 기록합니다.

리플레이는 원본 설정과 입력 해시를 검사하고 원래 순서·단조 시각으로 판정합니다. 기본 재생 속도는 1배속이며 `--fast`로 대기만 건너뛸 수 있습니다. 결과와 증거 ID를 대조하고 `replay_of`가 있는 별도 디렉터리에 판정·샘플·운전 회차를 재구성합니다.

### 측정값과 REAL 운전 회차

모의 입력 기록과 Jetson 수집 모두 다음 파일을 생성합니다.

- `samples.csv`에는 수집 구간과 확장 순번, `SAMPLE_TIME`의 수신 시각, 단위별 측정값과 유효성, 원본 프레임 ID를 남깁니다. 시간 프레임으로 부팅을 확인한 부분 샘플도 보존하며 결측·무효·만료 값은 빈 칸입니다. 행은 샘플 결합 완료 순서로 기록하므로 순번으로 정렬해서 볼 수 있습니다.
- `operations.jsonl`에는 REAL `RUNNING` 관측 구간별로 UUID, 시작·종료 경계와 관측 여부, 사유, 관련 원본 ID를 한 줄로 기록합니다. 직전의 최신 `STOPPED`를 확인했으면 `start_observed=true`입니다. 정지·고장·모드 변경을 관측하면 `end_observed=true`이며, 단절·만료·부팅 변경·기록 종료로 닫힌 구간은 `false`입니다. 이후 RUNNING을 다시 관측하면 새 UUID를 발급합니다.

샘플의 `SAMPLE_TIME`과 `ELECTRIC`이 같은 회차에서 수신됐을 때 REAL 샘플에는 `operation_id`, 수락된 TEST 회차 샘플에는 `run_id`를 붙입니다. 정지 중이나 회차를 확인할 수 없는 샘플은 두 칸을 비워 둡니다. 리플레이에서도 원본 증거 ID와 회차 ID가 유지되며, 이전 기록도 원본 입력에서 이 두 파일을 생성할 수 있습니다.

`raw_can.jsonl`의 `motor_boot`는 프레임에서 읽은 부팅 번호입니다. 부팅 번호나 모터 epoch가 없는 프레임은 `null`이며, 결합된 측정값의 부팅 번호와 원본 프레임 목록은 `samples.csv`에서 확인합니다.

수집 중 버퍼를 주기적으로 비우고, 판정 증거 및 기록 완료 시 파일을 동기화합니다. 저장에 실패하면 오류를 보고하고 완료 `manifest.json`을 게시하지 않습니다.

## TCP 통합 관측 서비스

`backend.monitor`는 CAN·FPGA UART 입력을 하나의 순서로 처리해 기록하고, 같은 상태와 시험 판정을 TCP로 보냅니다. Jetson과 장치가 연결되지 않은 환경에서는 진단 예제로 실행할 수 있습니다.

```sh
python3 -m backend.monitor .local/monitor-run \
  --fixture tests/fixtures/diagnostic-no-edge.jsonl --gpio-verified
```

클라이언트는 기본 `127.0.0.1:5000`에서 `hello → snapshot`을 받고 약 10Hz로 상태를 수신합니다. 판정을 디스크에 저장한 뒤 `event`를 보내며, TCP `event_id`로 `events.jsonl`의 전체 증거를 찾을 수 있습니다. 재접속하면 새 스트림에서 최근 상태와 판정을 받습니다. 명령 요청은 `READ_ONLY`로 응답합니다.

저장이 지연되어도 각 TCP 송신 시점의 상태·허가·측정값 유효 기간을 적용합니다.

예제는 원본 시각 간격을 따라 입력을 공급하며, 끝까지 처리한 뒤에도 TCP 관측을 유지합니다. 장치가 단절되면 해당 입력을 `OFFLINE`으로 표시하고 다른 입력과 TCP 관측을 계속합니다. FPGA UART를 다시 등록하려면 서비스를 재시작합니다. Ctrl+C 또는 SIGTERM으로 종료하면 관측한 입력까지만 마감하고, 정상적으로 동기화한 뒤 `manifest.json`을 게시합니다.

장비를 연결한 환경에서는 `--can "$CAN_IFACE" --uart "$FPGA_TTY"`로 수신 전용 관측을 실행할 수 있습니다. 실물 수신에서 시험은 기능 관측으로 기록합니다. 이 서비스의 자체 CAN 송신은 없습니다. 장치 준비 방법은 아래 절을 참조합니다.

CAN 소켓이 열리지 않거나 bus-off·소켓 오류로 단절되면, 직전 실패 또는 단절 후 1초, 2초, 4초 간격으로 최대 세 번 새 소켓을 엽니다. 호환 `MOTOR_HELLO`와 같은 epoch의 새 `MOTOR_STATE`를 받아야 `OK`로 돌아옵니다. 최신 모터 상태가 60초 동안 연속 관측되면 재시도 한도가 초기화됩니다. 재시도를 소진하면 서비스를 재시작해 다시 시도할 수 있습니다. Backend는 CAN 인터페이스 설정과 컨트롤러 재시작을 운영체제에 맡깁니다.

## Jetson 실물 연결 확인

Jetson의 Linux aarch64, Python 3.10 환경에서 아래 명령을 사용합니다. `CAN_IFACE`에는 125 kbit/s Classical CAN으로 준비된 SocketCAN 인터페이스 이름을, `FPGA_TTY`에는 Basys3 전용 `/dev/serial/by-id/...` 경로를 지정합니다. `ip -details link`와 `/dev/serial/by-id`에서 장치를 확인합니다. 수신기는 CAN 인터페이스 설정을 변경하지 않습니다. UART는 115200 8N1로 읽고 종료 시 이전 설정을 복구합니다.

```sh
python3 -m backend.hardware capture .local/can-rx --can "$CAN_IFACE" --seconds 5
python3 -m backend.hardware capture .local/fpga-rx --uart "$FPGA_TTY" --seconds 5
```

각 결과에 `ok`와 받은 프레임 수가 표시됩니다. CAN은 외부 노드의 호환 `MOTOR_HELLO`와 유효한 `MOTOR_STATE`, UART는 유효한 프레임을 각각 확인합니다. `--can`과 `--uart`를 함께 지정하면 두 입력을 같은 순번으로 기록합니다. 완료된 수신 기록은 `python3 -m backend.diagnostics replay .local/can-rx .local/can-rx-replay --fast`로 재생할 수 있습니다. 수신 중 애플리케이션은 CAN 프레임을 송신하지 않습니다. CAN 컨트롤러 자체의 ACK 동작은 인터페이스 모드에 따릅니다.

수신 점검과 통합 서비스는 `manifest.json`의 `can_rx`에 두 지표를 남깁니다. `socket_dropped_frames`는 커널이 수신 소켓 큐에서 떨어뜨렸다고 보고한 프레임 수입니다. `unobserved_sample_sequences`는 같은 수집 구간에서 수락된 `SAMPLE_TIME`의 순번 공백 수입니다. 두 지표는 서로 다른 위치의 관측값입니다. `inputs.jsonl`에는 드롭 수, 단절 사유, 소켓 재연결 결과가 남고, `raw_can.jsonl`에는 공백을 발견한 프레임의 `sample_seq_gap`이 남습니다. 큐 드롭을 감지하면 진행 중인 시험은 판정 불가로 처리하고 REAL 회차와 샘플 결합을 새 구간으로 분리합니다.

수신 확인을 마친 뒤, 모터 6V 전원을 물리적으로 분리하고 MCU와 CAN 트랜시버의 로직 전원은 유지합니다. 버스와 종단을 확인한 상태에서 별도로 아래 명령을 실행합니다.

```sh
python3 -m backend.hardware can-tx-probe --can "$CAN_IFACE" \
  --boot-id-file .local/jetson-operator-boot-id --motor-power-off
```

이 명령은 CAN 2.1 `OP_HELLO` 한 프레임을 제출합니다. `--motor-power-off`는 작업자의 확인입니다. `socket_echo`는 로컬 소켓의 확인이며, 실물 송신은 별도 CAN 수신 노드나 분석기에서 `0x701`과 출력된 payload를 대조해 확인합니다. 부팅 ID 파일은 동일 조작 노드의 재실행에서 재사용합니다. [Linux SocketCAN](https://docs.kernel.org/networking/can.html)

```sh
python3 -X dev -W error -m unittest discover -s tests -p 'check_hardware.py' -v
```
