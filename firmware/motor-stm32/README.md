# 모터 STM32

NUCLEO-F103RB의 PWM·엔코더·전원 계측, 모드·고장 상태, CAN 명령 처리와 FPGA 관측 GPIO를 구현하는 작업 영역입니다.

첫 작업은 모터 전원을 차단한 상태에서 CAN 입력으로 부팅 출력, 허가 소실, 현재 세션과 정지 우선 동작을 확인하는 것입니다. [통신 규격](../../docs/protocol.md)을 참조합니다.

프로젝트를 추가할 때 도구 버전과 빌드·다운로드 방법을 이 문서에 기록합니다. 빌드에 필요한 프로젝트 설정·소스·링커 파일을 함께 추적합니다.

## STM32CubeIDE 프로젝트

- 대상 보드는 NUCLEO-F103RB입니다. `motor_mcu.ioc`의 생성 환경은 STM32CubeMX 6.11.0과 STM32Cube FW_F1 V1.8.7입니다.
- STM32CubeIDE에서 **File → Import → Existing Projects into Workspace**를 선택하고 이 디렉터리의 `motor_mcu` 프로젝트를 가져옵니다. **Project → Build Project**로 빌드합니다.
- 로컬 ARM GCC로 펌웨어 빌드 및 링크를 확인했습니다. 보드의 ST-LINK를 연결하고 **Run** 또는 **Debug**로 다운로드합니다.
- 현재 CAN 설정은 125 kbit/s 내부 루프백 시험용입니다. 시작 시 송신한 시험 프레임을 FIFO0 수신값과 비교하고, 결과를 디버거에서 `canTestResult`로 확인할 수 있습니다.
