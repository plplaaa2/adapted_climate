# Adaptive Floor Heating Climate

[English](README.en.md)

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
![version](https://img.shields.io/badge/version-v1.0.0-blue.svg?style=for-the-badge)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-Support%20Me-F16061.svg?style=for-the-badge&logo=ko-fi)](https://ko-fi.com/plplaaa2)

Home Assistant용 학습형 바닥난방 Climate 커스텀 통합입니다. 실내 온도 센서와 난방 스위치를 연결해 기본 온도 제어를 제공하며, 운전 이력을 학습해 AUTO 모드에서 난방 시작과 정지를 보조합니다.

> [!IMPORTANT]
> **실험용:** 이 통합 구성요소는 개별 보일러에서만 실험 된 구성요소입니다.
> 
> 개별 보일러 외에 확실한 동작에 대한 보장을 하지 않으며 설치 및 사용에 관한 책임은 사용자에게 있습니다.
>



## 설치와 설정

[![Home Assistant에서 저장소 열기](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=plplaaa2&repository=adapted_climate&category=integration)

1. 위 버튼으로 설치하거나 [GitHub 저장소](https://github.com/plplaaa2/adapted_climate)를 HACS 사용자 지정 저장소에 **통합** 유형으로 추가합니다. 수동 설치 시 `custom_components/adaptive_floor_heating/`을 Home Assistant 설정 디렉터리의 `custom_components/`에 복사합니다.
2. Home Assistant를 재시작하고 **설정 → 기기 및 서비스 → 통합 추가**에서 **Adaptive Floor Heating Climate**를 선택합니다.
3. 단독 또는 통합 멀티룸 제어를 선택하고 히터 스위치와 실내 온도 센서를 연결합니다. 시작·정지 편차, 최소 ON/OFF 시간, HOME/AWAY 온도를 설정합니다. 외기별 예측에 사용할 실외 온도 센서는 선택 사항입니다.
4. 초기 모드는 `OFF`입니다. Climate 엔티티에서 `HEAT` 또는 `AUTO`를 선택해 난방을 시작합니다.

## 운전 모드

| 모드 | 동작 |
| --- | --- |
| `OFF` | 자동 난방 제어를 끕니다. |
| `HEAT` | 설정온도와 시작·정지 편차로 난방을 제어합니다. |
| `AUTO` | 기본 제어에 학습 기반 예측을 추가합니다. |

- **예측 ON**을 켜면 예상 냉각과 난방 반응 지연을 고려해 미리 가동합니다.
- **예측 OFF**는 가동시간 모델로 초기 정지를 예약하고, 가동합산 예측의 근거가 충분해지면 예상 최고온도로 정지를 판단합니다. 두 모델의 근거가 부족하면 기본 온도 제어를 사용합니다.
- **HOME/AWAY**는 설정온도를 바꿉니다. 센서·스위치 오류, 과열, 외부 조작 시에는 난방을 중지하거나 시작을 보류합니다. 오류 잠금이 걸리면 원인 해소와 히터 OFF 확인 후 `HEAT` 또는 `AUTO`를 다시 선택합니다.

### 일출 절약

**예측 운전 설정 → 일출 절약**을 켜고 시작·종료 시각을 각각 저장합니다. 지정 시간에는 AUTO의 예측 정지 목표가 **설정온도 + 상한 허용 편차**에서 **설정온도**로 내려갑니다. Home Assistant 현지 시각을 따르며, 자정을 넘길 수 있습니다. 두 시각이 같으면 적용하지 않습니다.

## 대시보드와 학습

### 초기 3회 학습

새로 설치하거나 학습을 리셋한 뒤에는 평소처럼 난방을 운전하면 자동으로 학습합니다. 가동시간 기반 예측 OFF에는 히터 가동 시간과 정지 후 잔열로 도달한 실제 최고온도를 관측하고 학습 조건을 통과한 난방 사이클이 최소 3회 필요합니다. 학습에서 제외된 회차는 이 횟수에 포함되지 않습니다.

예측 OFF에 쓸 근거가 부족하면 설정온도와 허용 편차에 따른 기본 정지 기준을 사용합니다. 3회가 쌓이면 현재 운전 조건에 적용할 수 있는 가동시간 예측이 있는지 판단합니다. 예측 ON은 냉각과 난방 반응 지연 학습을 바탕으로 별도로 판단합니다.

- **예측 분석**에서 초기 정지 예약, 현재 판단 모델, 예상·실제 최고온도를 확인합니다.
- **학습 분석**에서 최근·장기 학습과 회차별 학습 보류 사유를 확인합니다. **최고점 관측 시간 한도**는 예측 운전 설정에서 조정합니다.
- **학습 리셋**은 히터 OFF가 확인되고 대기 중인 스위치 명령이 없을 때 해당 기기의 학습과 자체 예측 비교 기록을 초기화합니다. HA Recorder 이력과 기기 설정은 유지합니다.

## 개발 테스트

Hassfest는 비공개 저장소에서도 실행하며, HACS 검증은 공개 저장소에서 실행합니다. 이 저장소에는 라이선스가 없어 HACS의 `license` 검사만 제외합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests/components/adaptive_floor_heating -p "test_*.py" -v
```
