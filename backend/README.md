# Backend

Jetson Orin Nano에서 실행하는 CAN DO 제어·진단 Python 서비스 작업 영역입니다. Qt TCP 연결, CAN 명령·운전 허가 발행, CAN·FPGA UART 수집, 시험 판정, 로그·리플레이를 담당합니다.

## 전체 소프트웨어 검증

저장소 루트에서 다음 명령을 실행합니다.

```sh
python3 -X dev -W error -m unittest discover -s tests -p 'check_*.py' -v
python3 -m compileall -q backend tests
git diff --check
```

자동 테스트는 TCP 조작, 모의 CAN, 가상 시리얼 포트의 UART 수신·복구, 판정·기록·리플레이와 통합 서비스를 검증합니다. 9c는 가상 데이터그램 소켓에서 명령·허가 송신, STOP 선점, 전원 차단 조건과 장애 복구를 검증합니다. 읽기 전용 TCP의 메시지 순서·전송 주기·재접속은 아래 독립 클라이언트로 확인합니다. 실물 수신·송신은 [Jetson 실물 연결 확인](#jetson-실물-연결-확인)의 절차를 따릅니다.

## TCP·CAN 목업

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

검증 CLI는 디코딩 결과·오류·관측된 회차를 JSON 줄로 출력합니다. 공통 예제는 `INJECT` 한 프레임이라 `MISSING_BASELINE`이 함께 표시됩니다. 파일 입력에는 읽은 시점의 단조 시각을 부여하고, 보드 시간은 프레임의 `tick_us`로 유지합니다. CAN과 결합한 판정은 [시험 판정·기록·리플레이](#시험-판정기록리플레이)에서 확인합니다.

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

### 기록 운영

- `backend.monitor`, `backend.diagnostics record`, `backend.hardware capture`는 확인한 빌드 ID(`--motor-build`, `--fpga-build`)와 펌웨어 설정(`--encoder-counts-per-rev`, `--rpm-max`, `--feedback-timeout-ms`)을 매니페스트에 남깁니다. 생략한 값은 `null`입니다. 관측·판정 서비스와 예제 기록의 `--gpio-limit-us`는 판정 설정이며 리플레이에도 적용됩니다.
- 로그는 100MiB에서 행 단위로 분할합니다. 단일 행이 한도보다 크면 그 행을 하나의 파일에 유지합니다. 첫 파일은 기존 이름을 쓰고 두 번째부터 `.000002`를 붙입니다. CSV 헤더는 첫 파일에 있으며, 자동 리플레이는 매니페스트의 `log_segments` 순서를 사용합니다. 오래된 파일은 자동 삭제하지 않습니다.
- 통합 모니터는 여유 공간이 1GiB 이하일 때 `services.log=DEGRADED`와 `recording.free_bytes`를 보고합니다. 이 구간에서 시작한 시험은 `INCONCLUSIVE / RECORDING_SPACE_LOW`로 기록하고 경고 원본 ID를 판정 근거에 연결합니다. 공간이 회복되면 이후 시험을 정상 판정할 수 있습니다. 수신 전용 캡처 CLI에서는 용량 경고를 로그에 남깁니다.
- 통합 모니터에서 쓰기·동기화 실패 또는 기록 큐 초과가 발생하면 수집·CAN 조작을 중단하고, 진행 중이거나 저장이 확정되지 않은 시험을 `INCONCLUSIVE / RECORDING_FAILED`로 표시합니다. TCP 조회는 종료 신호까지 유지합니다. `dropped_records`는 미기록이 확인된 건수이고 `unconfirmed_records`는 그보다 넓은 범위로 디스크 동기화 여부가 확인되지 않은 입력까지 포함합니다. 실패 기록은 가능한 경우 `recording-failure.json`에 남기며 서비스 재개에는 재시작이 필요합니다.

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

클라이언트는 기본 `127.0.0.1:5000`에서 `hello → snapshot`을 받고 약 10Hz로 상태를 수신합니다. 판정을 디스크에 저장한 뒤 `event`를 보내며, TCP `event_id`로 `events.jsonl`의 전체 증거를 찾을 수 있습니다. 재접속하면 새 스트림에서 최근 상태와 판정을 받습니다. 기본 관측 모드의 명령 요청은 `READ_ONLY`로 응답합니다.

첫 스냅샷 뒤 현재 서비스 상태를 보내고, 이후 달라진 서비스 상태와 고장·링크 이벤트를 전달합니다. 클라이언트는 `event.data.category`로 시험 판정과 장치 이벤트를 구분합니다. [TCP 메시지 규격](../docs/protocol.md#qt와-backend)

저장이 지연되어도 각 TCP 송신 시점의 상태·허가·측정값 유효 기간을 적용합니다.

예제는 원본 시각 간격을 따라 입력을 공급하며, 끝까지 처리한 뒤에도 TCP 관측을 유지합니다. 장치가 단절되면 해당 입력을 `OFFLINE`으로 표시하고 다른 입력과 TCP 관측을 계속합니다. Ctrl+C 또는 SIGTERM으로 종료하면 관측한 입력까지만 마감하고, 정상적으로 동기화한 뒤 `manifest.json`을 게시합니다.

장비를 연결한 환경에서는 `--can "$CAN_IFACE" --uart "$FPGA_TTY"`로 수신 전용 관측을 실행할 수 있습니다. 실물 수신에서 시험은 기능 관측으로 기록합니다. 기본 관측 모드는 CAN을 수신합니다. 장치 준비 방법은 아래 절을 참조합니다.

CAN 소켓이 열리지 않거나 bus-off·소켓 오류로 단절되면, 직전 실패 또는 단절 후 1초, 2초, 4초 간격으로 최대 세 번 새 소켓을 엽니다. 호환 `MOTOR_HELLO`와 같은 epoch의 새 `MOTOR_STATE`를 받아야 `OK`로 돌아옵니다. 최신 모터 상태가 60초 동안 연속 관측되면 재시도 한도가 초기화됩니다. 재시도를 소진하면 서비스를 재시작해 다시 시도할 수 있습니다. Backend는 CAN 인터페이스 설정과 컨트롤러 재시작을 운영체제에 맡깁니다.

FPGA UART도 포트 열기 실패나 연결 종료 후 같은 경로를 최대 세 번 다시 엽니다. CAN과 UART의 재시도 한도는 독립적입니다. `/dev/serial/by-id/...`를 사용하면 같은 장치의 tty 번호가 바뀌어도 사라진 링크가 복구된 뒤 재등록할 수 있습니다. 포트를 열 때 읽지 않은 입력을 비우고, 재등록 시 새 FPGA 세션에서 CRC가 유효한 `STATUS`를 기다립니다. 정상 STATUS가 60초 동안 연속 관측되면 UART 재시도 한도를 초기화합니다.

실물 수신에서 진행 중인 시험에 유효한 `STATUS`가 500ms 동안 없으면 `INCONCLUSIVE`로 마감하며, 리플레이는 기록 당시와 같은 감시 설정을 사용합니다. 순번이나 보드 시각이 역행해 격리된 세션은 포트 재연결이나 서비스 재시작으로 복구합니다.

### 읽기 전용 TCP 리플레이

```sh
python3 -m backend.monitor --replay .local/monitor-run --port 5000
```

완료 매니페스트와 원본 입력 해시를 확인한 뒤 첫 TCP 접속에서 원래 수신 간격으로 재생합니다. 이벤트 송신 대기열이 가득 차면 재생을 일시 정지하고 이후에도 원래 입력 간격을 유지합니다. 분할 기록도 지원하며, 재생 중 각 입력 묶음이 시작 시 검사한 내용과 일치하는지 확인합니다. 원본 판정과 근거가 일치한 시험 결과만 전송합니다. 재접속은 같은 재생 지점의 최신 상태를 받고, EOF 뒤에는 마지막 관측 시각의 상태가 유지됩니다. 처음부터 재생하려면 프로세스를 재시작합니다. 기록 디렉터리를 읽기만 하며 장치 연결 및 CAN 송신은 수행하지 않습니다.

### 전원 차단 조작과 진단 기록 통합

모터 6V 전원이 차단된 환경에서 CAN 조작과 UART 진단을 한 TCP 연결로 확인할 수 있습니다.

```sh
python3 -m backend.monitor .local/control-run \
  --can "$CAN_IFACE" --uart "$FPGA_TTY" \
  --motor-power-off --boot-id-file .local/jetson-operator-boot-id
```

로컬 클라이언트는 같은 `stream_id`에서 `control_result`, `snapshot`, 판정 `event`를 받습니다. 조작은 TEST 모드와 공급 차단 확인을 적용하며 `START_REAL`을 거부합니다. 단독 `can-control`과 같은 부팅 ID 파일을 사용합니다. 실물 입력의 GPIO 시간 판정에는 기존 기능 관측 제한을 적용합니다.

`inputs.jsonl`과 `raw_can.jsonl`의 `source=can_tx`는 소켓 제출을 나타냅니다. CAN 명령과 제어기가 매칭한 MCU 응답을 `detail.request_id`로 연결하고, 시험 판정의 `evidence_ids`가 관련 프레임을 참조합니다. 같은 명령의 `TEST_ARMED` 상태와 실패 응답이 함께 관측되면 `INCONCLUSIVE`로 기록합니다. STOP은 제어기가 제출한 억제·명령을, RESET은 MCU 완료 응답을 중단 판정에 사용합니다. `END` 전에 제출한 RESET의 완료 응답이 `END` 뒤에 오거나 판정 마감까지 결과가 없으면 `INCONCLUSIVE`로 기록합니다. 원본을 리플레이하면 입력 순서와 판정 근거가 재현됩니다.

수집 작업의 예외로 서비스가 중단되면 판정에 넘기기 전인 입력과 큐에 남은 원본을 `inputs.jsonl`에 순서대로 저장합니다. 이때 추가 판정과 완료 표시인 `manifest.json`의 생성은 생략합니다.

## Jetson 실물 연결 확인

Jetson의 Linux aarch64, Python 3.10 환경에서 아래 명령을 사용합니다. `CAN_IFACE`에는 125 kbit/s Classical CAN으로 준비된 SocketCAN 인터페이스 이름을, `FPGA_TTY`에는 Basys3 전용 `/dev/serial/by-id/...` 경로를 지정합니다. `ip -details link`와 `/dev/serial/by-id`에서 장치를 확인합니다. 수신기는 CAN 인터페이스 설정을 변경하지 않습니다. UART는 115200 8N1로 읽고 종료 시 이전 설정을 복구합니다.

```sh
python3 -m backend.hardware capture .local/can-rx --can "$CAN_IFACE" --seconds 5
python3 -m backend.hardware capture .local/fpga-rx --uart "$FPGA_TTY" --seconds 5
```

각 결과에 `ok`와 받은 프레임 수가 표시됩니다. CAN은 외부 노드의 호환 `MOTOR_HELLO`와 유효한 `MOTOR_STATE`, UART는 유효한 프레임을 각각 확인합니다. `--can`과 `--uart`를 함께 지정하면 두 입력을 같은 순번으로 기록합니다. 완료된 수신 기록은 `python3 -m backend.diagnostics replay .local/can-rx .local/can-rx-replay --fast`로 재생할 수 있습니다. 수신 중 애플리케이션은 CAN 프레임을 송신하지 않습니다. CAN 컨트롤러 자체의 ACK 동작은 인터페이스 모드에 따릅니다.

UART 점검은 포트를 열 때 이미 대기 중인 입력을 비운 뒤 새로 읽은 프레임을 집계합니다. CRC·버전·필드 오류, 전송·시험 순번 공백, 세션 격리, FPGA의 overflow/invalid 상태 또는 진행 중 시험의 STATUS 만료를 관측하면 실패로 표시합니다. `fpga_invalid_observations`는 같은 세션에 유지된 오류 상태를 포함해 `FPGA_INVALID`가 붙은 관측 수를 집계합니다. 개별 프레임 근거는 `raw_fpga.jsonl`에 남깁니다.

수신 점검과 통합 서비스는 `manifest.json`의 `can_rx`에 두 지표를 남깁니다. `socket_dropped_frames`는 커널이 수신 소켓 큐에서 떨어뜨렸다고 보고한 프레임 수입니다. `unobserved_sample_sequences`는 같은 수집 구간에서 수락된 `SAMPLE_TIME`의 순번 공백 수입니다. 두 지표는 서로 다른 위치의 관측값입니다. `inputs.jsonl`에는 드롭 수, 단절 사유, 소켓 재연결 결과가 남고, `raw_can.jsonl`에는 공백을 발견한 프레임의 `sample_seq_gap`이 남습니다. 큐 드롭을 감지하면 진행 중인 시험은 판정 불가로 처리하고 REAL 회차와 샘플 결합을 새 구간으로 분리합니다.

수신 확인을 마친 뒤, 모터 6V 전원을 물리적으로 분리하고 MCU와 CAN 트랜시버의 로직 전원은 유지합니다. 버스와 종단을 확인한 상태에서 별도로 아래 명령을 실행합니다.

```sh
python3 -m backend.hardware can-tx-probe --can "$CAN_IFACE" \
  --boot-id-file .local/jetson-operator-boot-id --motor-power-off
```

이 명령은 CAN 2.1 `OP_HELLO` 한 프레임을 제출합니다. `--motor-power-off`는 작업자의 확인입니다. `socket_echo`는 로컬 소켓의 확인이며, 실물 송신은 별도 CAN 수신 노드나 분석기에서 `0x701`과 출력된 payload를 대조해 확인합니다. 부팅 ID 파일은 동일 조작 노드의 재실행에서 재사용합니다. [Linux SocketCAN](https://docs.kernel.org/networking/can.html)

### 전원 차단 CAN 조작 점검

송신 프로브가 종료된 뒤 같은 부팅 ID 파일을 사용해 로컬 TCP 조작 점검을 실행합니다. 프로브와 조작 서버는 실행 중 부팅 ID 파일의 잠금을 유지하며, 같은 파일을 사용하는 두 번째 실행은 시작 오류를 반환합니다.

```sh
python3 -m backend.hardware can-control --can "$CAN_IFACE" \
  --boot-id-file .local/jetson-operator-boot-id --motor-power-off
```

서버는 기본 `127.0.0.1:5000`에서 연결을 받습니다. TCP 클라이언트는 `hello`에서 받은 새 `stream_id`로 heartbeat를 보내고, 새 `request_id`를 사용해 `ARM_TEST`, `STOP`, `RESET` 요청을 보냅니다. MCU가 TEST 모드와 모터 공급 차단을 보고할 때만 준비 허가를 보냅니다. 이 점검에서 `START_REAL`은 `POWER_OFF_TEST_ONLY`로 거부합니다.

SocketCAN은 `OP_HELLO`(`0x701`), 허가·출력 억제 `OP_PERMIT`(`0x080`), `COMMAND`(`0x100`)를 비차단으로 제출합니다. 정상 허가 주기는 50ms입니다. 제출 대기 50ms가 만료되면 대기 명령을 폐기하고, 제출 후 300ms 동안 MCU 결과가 없으면 `UNKNOWN_OUTCOME`으로 종료합니다. CAN 재연결이나 MCU 부팅 번호 변경 후에는 `OP_HELLO`를 다시 보내고, 출력 억제 이후의 새 epoch 정지 상태와 새 heartbeat를 확인합니다. 새 명령 요청이 있어야 시험 준비를 다시 시도합니다. 종료 시 가능한 경우 출력 억제 프레임을 한 번 제출합니다.

수신 큐에 프레임이 남아 있어도 출력 억제를 먼저 제출할 수 있습니다. 이때 처음 보낸 억제는 복구 판정의 기준으로 삼지 않으며, 큐를 비운 뒤 다시 제출한 억제에 대해 새 정지 상태를 확인합니다.

수신 큐 드롭이나 SocketCAN 패킷 형식 오류로 연결을 끊을 때는 소켓을 닫기 전에 출력 억제 제출을 한 번 시도합니다. 재연결 후에는 새 억제와 정지 상태를 확인합니다.

조작 서버의 TCP 상태에는 자기 소켓에서 에코된 `OP_HELLO`와 `OP_PERMIT`를 사용합니다. 수신 전용 점검은 버스에서 들어온 조작 프레임을 계속 관측합니다.

실물 검증에서는 CAN 분석기로 허가 주기와 STOP·TCP 단절 직후 억제 프레임이 도달한 시점을 확인합니다. MCU의 `COMMAND_RESULT`(`0x110`)와 `MOTOR_STATE`를 각각 대조하고, CAN 단절·복구 및 프로세스 재시작 이후 이전 명령이 실행되지 않는지 확인합니다. 소켓 제출과 로컬 에코는 Jetson에서 확인할 수 있습니다. 제출된 프레임의 실제 버스 도달 시점과 드라이버·어댑터 큐의 잔류 동작은 실물 계측이 필요합니다.

```sh
python3 -X dev -W error -m unittest discover -s tests -p 'check_hardware.py' -v
python3 -X dev -W error -m unittest discover -s tests -p 'check_live_can_control.py' -v
```
