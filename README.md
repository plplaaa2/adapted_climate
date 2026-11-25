# Adaptive Floor Heating Climate

[English](README.en.md) · [변경 내역](CHANGELOG.md)

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
![version](https://img.shields.io/badge/version-v1.2.1-blue.svg?style=for-the-badge)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-Support%20Me-F16061.svg?style=for-the-badge&logo=ko-fi)](https://ko-fi.com/plplaaa2)

Home Assistant용 학습형 바닥난방 Climate 커스텀 통합입니다. 실내 온도 센서와 난방 스위치 또는 기존 온도조절기를 연결해 기본 온도 제어를 제공하며, 운전 이력을 학습해 AUTO 모드에서 난방 시작과 정지를 보조합니다.

> [!IMPORTANT]
> **실험용:** 이 통합 구성요소는 개별 보일러에서만 실험 된 구성요소입니다.
> 
> 개별 보일러 외에 확실한 동작에 대한 보장을 하지 않으며 설치 및 사용에 관한 책임은 사용자에게 있습니다.
>
> **업데이트 주의:** 이 통합 구성요소는 학습 중에 업데이트 하면 학습이 중지 됩니다.
>
> 업데이트는 학습이 종료 된 후 진행해 주시기 바랍니다.


## 설치와 설정

[![Home Assistant에서 저장소 열기](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=plplaaa2&repository=adapted_climate&category=integration)

1. 위 버튼으로 설치하거나 [GitHub 저장소](https://github.com/plplaaa2/adapted_climate)를 HACS 사용자 지정 저장소에 **통합** 유형으로 추가합니다. 수동 설치 시 `custom_components/adaptive_floor_heating/`을 Home Assistant 설정 디렉터리의 `custom_components/`에 복사합니다.
2. Home Assistant를 재시작하고 **설정 → 기기 및 서비스 → 통합 추가**에서 **Adaptive Floor Heating Climate**를 선택합니다.
3. **스위치형** 또는 **Climate형**을 선택합니다. 스위치형은 단독 또는 통합 멀티룸을 선택하고 스위치와 실내 온도 센서를 연결합니다. Climate형은 온도조절기 한 대와 실내 온도 센서를 연결하고 목표온도 하한과 상한을 입력합니다. 이어서 시작·정지 편차, 최소 ON/OFF 시간, HOME/AWAY 온도를 설정합니다. 선택 센서에서 외기 온도와 일출 학습용 날씨 엔티티를 지정할 수 있습니다.
4. 초기 모드는 `OFF`입니다. Climate 엔티티에서 `HEAT` 또는 `AUTO`를 선택해 난방을 시작합니다.

## 첫 운전

1. 초기 모드는 `OFF`입니다. 연결한 센서의 현재 온도와 실제 기기 OFF 상태를 확인합니다.
2. `HEAT`로 기본 제어를 확인한 뒤 `AUTO`로 전환합니다. HOME/AWAY는 목표온도를 바꾸는 프리셋입니다.
3. AUTO 예측 OFF에는 실제 가동시간과 잔열 최고점이 확인된 유효 사이클이 최소 **3회** 필요합니다. 근거가 부족하면 기본 온도 제어를 사용합니다.
4. 사이드바 **바닥난방**에서 해당 방의 운전 상태, 예측 분석과 학습 분석을 확인합니다.

최소 ON/OFF 대기 때문에 요청이 즉시 실행되지 않을 수 있습니다. Climate형은 기존 온도조절기에 하한·상한을 보내므로 실제 난방 상태도 확인하세요. 업데이트·설정 변경은 난방과 잔열·냉각 관측이 끝난 뒤 진행합니다.

Climate형은 ON·OFF 명령 확인 대기 시간을 옵션에서 설정합니다. 기본값은 1초입니다. 실내·실외 온도와 선택한 날씨 센서는 시작 시 및 5분 간격으로 확인하며, 한 번의 누락은 마지막 정상값으로 보완합니다.

두 번 연속 온도 센서를 확인할 수 없으면 난방을 정지하고 해당 학습 회차를 제외합니다. 자세한 내용은 [초기 설정](docs/setup.md)과 [운전과 학습](docs/operation.md)을 참고하세요.

## 사용자 가이드

관리자는 **바닥난방 → 학습 분석 → 마이그레이션**에서 선택한 방의 최고점 타임아웃, 난방기 통신 끊김, 일출 절약 난방·냉각 기록을 재검사할 수 있습니다. 대상별 복구 조건은 [유지 관리 가이드](docs/maintenance.md#학습-기록-마이그레이션)를 참고하세요.

자세한 설명은 [문서 목차](docs/README.md)에서 확인할 수 있습니다.

| 문서 | 주요 내용 |
| --- | --- |
| [초기 설정](docs/setup.md) | 제어 방식, 센서·기기 요구 사항, 기본값, 첫 운전 확인 |
| [운전과 학습](docs/operation.md) | 예측 ON/OFF, 초기 학습, 최고점 확정, 일출 절약 |
| [유지 관리](docs/maintenance.md) | 설정 변경, 업데이트, 백업, 기록 보존, 복구와 리셋 |
| [문제 해결](docs/troubleshooting.md) | 난방 대기, 학습 제외, 오류 복구와 진단 JSON 다운로드 |

## 개발 테스트

Hassfest는 비공개 저장소에서도 실행하며, HACS 검증은 공개 저장소에서 실행합니다. 이 저장소에는 라이선스가 없어 HACS의 `license` 검사만 제외합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests/components/adaptive_floor_heating -p "test_*.py" -v
```
