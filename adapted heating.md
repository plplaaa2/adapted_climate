
> 이 파일은 초기 설계 초안과 체크리스트입니다. 2026-10-02 확정한 모델 선택, AWAY 기반 Cold, 실제 하강 기반 Peak 및 OFF 응답 예측의 최신 규칙은 [모델별 작동 로직](docs/model_operation.md)을 참조하세요. 아래의 HOME OFF 시간 기반 Cold 및 셀렉터 제거 계획은 최신 합의에 적용하지 않습니다. Current/Long-term 분리와 환경 변화 적응 등 미구현 연구 항목은 초안으로 보존합니다.

# Adaptive Floor Heating
## Curve Learning System Specification

### 1. 목적

Adaptive Floor Heating의 핵심 목적은 단순 히스테리시스 온도조절기가 아니라, 실제 주택의 바닥난방 열관성을 관측하고 학습하여 난방 ON/OFF 시점을 예측적으로 제어하는 것이다.

바닥난방은 보일러를 끈 이후에도 바닥과 배관에 저장된 열 때문에 실내온도가 계속 상승하며, 반대로 보일러를 켠 직후에도 바닥이 데워지는 동안 상당한 반응 지연이 존재한다.

따라서 다음과 같은 단순 제어는 적합하지 않다.

```text
현재온도 < 목표온도 → ON
현재온도 >= 목표온도 → OFF
```

대신 시스템은 실제 난방 사이클의 시간-온도 궤적을 학습한다.

```text
실내온도 센서
      ↓
난방 사이클 관측
      ↓
5분 단위 Thermal Curve 학습
      ↓
미래 온도 궤적 예측
      ↓
Predictive ON / Predictive OFF
```

---

# 2. 기본 제어 모델

Home Assistant Climate 엔티티는 실내온도 기준으로 동작한다.

```text
current_temperature = 실내온도
target_temperature  = 사용자가 원하는 실내온도
```

보일러 또는 난방 밸브는 기본적으로 binary heater actuator로 본다.

필수 입력은 다음 두 가지이다.

```text
temperature_sensor
heater switch
```

외기온도, 공급수온, 환수온도 등은 MVP 커브 제어의 필수 입력이 아니다.

---

# 3. 열반응 모델의 기본 개념

시스템이 학습하는 것은 단일 `heating_rate`가 아니다.

학습 대상은 다음과 같다.

> 특정 초기 열상태에서 난방을 시작했을 때 실내온도가 시간에 따라 어떻게 변하고, 난방 정지 후 잔열 때문에 얼마나 더 상승하며, 최고점 이후 얼마나 빠르게 냉각되는가.

전체 한 사이클의 물리적 구조는 다음과 같다.

```text
               Heater ON
                   │
                   │ Heating Response
                   │
               Heater OFF
                   │
                   │ Coasting / Residual Heat
                   │
                 PEAK
                   │
                   │ Cooling Response
                   │
               Next ON
```

따라서:

```text
ON → OFF → PEAK
```

구간은 하나의 Heating Response로 취급한다.

```text
PEAK → Next ON
```

구간은 Cooling Response로 취급한다.

OFF 시점에서 Heating Curve를 자르면 안 된다.

Predictive OFF에서 가장 중요한 정보가 바로 OFF 이후의 잔열 상승이기 때문이다.

---

# 4. 5분 버킷

커브 학습의 기본 해상도는 5분이다.

```text
BUCKET = 5 minutes
```

실제 센서 보고 주기는 더 짧아도 된다.

5분 경계마다 당시 마지막으로 유효한 실내온도를 사용한다.

예:

```text
난방 시작 온도 = 21.0°C

0분      21.0
5분      21.0
10분     21.0
15분     21.1
20분     21.2
25분     21.4
```

커브에는 절대온도 대신 변화량을 저장한다.

```text
0~5분      +0.0
5~10분     +0.0
10~15분    +0.1
15~20분    +0.1
20~25분    +0.2
```

온도가 변하지 않은 버킷도 반드시 저장한다.

```text
delta = 0.0
```

이는 누락 데이터가 아니라 바닥난방의 반응 지연을 나타내는 중요한 정보이다.

---

# 5. Heating Curve 종류

MVP에서는 세 종류의 Heating Curve를 사용한다.

```text
COLD_HEATING
WARM_HEATING
PREDICTIVE_WARM_HEATING
```

이들은 서로 다른 물리 시스템이 아니다.

같은 바닥난방 시스템이 **서로 다른 초기 축열상태**에서 출발한 경우를 구분한 것이다.

---

## 5.1 Cold Heating

바닥 및 구조체의 축열이 상당 부분 소진된 상태에서 시작한 난방이다.

Cold 판정 기준:

```text
HOME 상태에서 heater OFF 지속시간 >= 3시간
→ COLD_HEATING
```

또는:

```text
AWAY → HOME 전환 후 첫 난방
→ COLD_HEATING
```

Cold 판정 시간 기준은 다음과 같이 명시한다.

```text
COLD_OFF_THRESHOLD = 3 hours
```

현재 구현의 1시간 기준은 3시간으로 변경한다.

---

# 6. Away 처리

AWAY 상태에서 발생하는 난방 사이클은 정상 학습 데이터에서 제외한다.

이유:

- 동파방지
- 낮은 Away 목표온도
- 장시간 부재
- 불규칙한 난방
- 정상 재실 운전과 다른 조건

때문에 HOME 상태와 동일한 모델에 넣으면 Heating/Cooling Curve를 오염시킬 수 있다.

규칙:

```text
preset == AWAY
→ curve learning disabled
```

AWAY 상태에서 필요하다면 관측은 유지할 수 있지만:

```text
Current Curve 업데이트 금지
Long-term Curve 업데이트 금지
```

한다.

AWAY → HOME 전환 시:

```text
다음 첫 난방 사이클
→ COLD_HEATING
```

으로 분류한다.

그 첫 Cold 사이클이 끝난 이후 정상 HOME 분류로 복귀한다.

---

# 7. Warm Heating

HOME 상태에서 이전 난방의 축열이 남아 있는 일반적인 재가동이다.

```text
heater OFF < 3시간
AND
normal threshold start
→ WARM_HEATING
```

예:

```text
Target = 23.0°C
Lower threshold = 22.5°C

실내온도가 실제 22.5°C까지 내려감
→ Heater ON
→ WARM_HEATING
```

Eco 운전에서 가장 자주 생성될 것으로 예상된다.

단, Warm과 Eco는 동일 개념이 아니다.

Warm은 열상태 분류이고 Eco는 운전 정책이다.

---

# 8. Predictive Warm Heating

실내온도가 일반 난방 시작 threshold에 도달하기 전에 예측적으로 난방을 시작한 경우이다.

```text
heater OFF < 3시간
AND
start_reason == PREDICTIVE_START
→ PREDICTIVE_WARM_HEATING
```

주로 Balanced / Comfort 운전에서 생성된다.

예:

```text
Target = 23.0°C
Normal start threshold = 22.5°C

현재 = 22.8°C
현재 냉각 추세 + 난방 반응지연을 고려할 때
22.5°C 이하로 떨어질 것으로 예상

→ 선제적으로 ON
→ PREDICTIVE_WARM_HEATING
```

---

# 9. Heating Curve 분류 우선순위

분류는 반드시 다음 순서로 처리한다.

```text
1. preset == AWAY
   → 학습하지 않음

2. AWAY → HOME 이후 첫 난방
   → COLD_HEATING

3. HOME AND heater OFF >= 3시간
   → COLD_HEATING

4. HOME AND start_reason == PREDICTIVE_START
   → PREDICTIVE_WARM_HEATING

5. 그 외
   → WARM_HEATING
```

즉 Cold 여부가 Predictive Start보다 우선한다.

장시간 냉각 후 예측적으로 재가동했다고 해서 Predictive Warm으로 분류해서는 안 된다.

---

# 10. Cooling Curve

Cooling은 하나의 공통 모델만 사용한다.

```text
COOLING
```

MVP에서는 Cold/Warm/Predictive Warm별 Cooling Curve를 따로 만들지 않는다.

Cooling Curve 시작점:

```text
confirmed PEAK
```

끝점:

```text
다음 Heater ON
```

또는 데이터 품질상 종료 조건 발생 시점이다.

Cooling Curve도 동일하게 5분 버킷으로 저장한다.

예:

```text
Peak      23.4
+5 min    23.4
+10 min   23.3
+15 min   23.3
+20 min   23.2
```

저장값:

```text
0.0
0.0
-0.1
0.0
-0.1
```

---

# 11. Peak 검출

Heater OFF 이후 가장 높은 실내온도를 Peak로 본다.

기본 알고리즘:

```text
OFF
 ↓
현재 최고온도를 Peak Candidate로 기록
 ↓
더 높은 온도 보고
→ Peak Candidate 갱신
 ↓
10분 동안 더 높은 온도가 나타나지 않음
 ↓
Peak 확정
```

기본 settle time:

```text
PEAK_SETTLE_TIME = 10 minutes
```

현재 구현 방식은 유지 가능하다.

다만 다음 두 설정은 같은 값이더라도 서로 다른 의미의 상수로 분리한다.

```text
MAX_PEAK_WAIT
MAX_COOLING_OBSERVATION
```

초기값은 둘 다 3시간을 사용할 수 있다.

```text
MAX_PEAK_WAIT = 3 hours
MAX_COOLING_OBSERVATION = 3 hours
```

---

# 12. Raw Cycle 저장

모든 유효 또는 분석 가치가 있는 사이클은 Raw Cycle 데이터로 저장한다.

예상 메타데이터:

```text
cycle_id
curve_type
start_reason
off_reason
start_time
off_time
peak_time
end_time
mode
preset
accepted
quality_reason
```

5분 버킷:

```text
cycle_id
curve_type
bucket_index
delta_c
```

Raw bucket의 기본 보존기간:

```text
7 days
```

7일 이후:

```text
raw 5-minute buckets → 삭제 가능
cycle metadata       → 유지 가능
```

한다.

Raw 데이터는:

- 디버깅
- 이상치 분석
- Current Curve 검증
- 향후 학습 알고리즘 변경
- UI graph

에 사용한다.

---

# 13. Memory Architecture

커브 학습 메모리는 두 단계로 분리한다.

```text
Raw Cycle
    ↓
Current Curve
    ↓
Long-term Curve
```

최종 구조:

```text
최근 실제 Cycle
      │
      ├──────────────→ Raw 7일 저장
      │
      ↓
Current Curve
빠르게 변화하는 현재 열특성
      │
      │ 안정된 Current만 천천히 전달
      ↓
Long-term Curve
주택 자체의 장기 열특성 기억
```

각 Curve Type마다 독립적으로 가진다.

```text
COLD
├─ Current
└─ Long-term

WARM
├─ Current
└─ Long-term

PREDICTIVE_WARM
├─ Current
└─ Long-term

COOLING
├─ Current
└─ Long-term
```

---

# 14. Current Curve

Current Curve는 최근 환경 변화에 빠르게 적응하기 위한 모델이다.

현재 구현의 EWMA 방식을 Current Curve에 사용할 수 있다.

초기 예:

```text
new_current
=
old_current × 0.8
+
new_cycle × 0.2
```

즉:

```text
CURRENT_ALPHA = 0.2
```

를 초기값으로 사용한다.

Current Curve는 다음과 같은 변화를 빠르게 따라가야 한다.

- 계절 변화
- 갑작스러운 한파
- 실내 생활 패턴
- 바닥 초기 상태 변화
- 난방 시스템 조건 변화

Raw 7일 데이터와 Current Curve는 별개의 데이터다.

---

# 15. Long-term Curve

Long-term Curve는 주택의 장기적인 열적 특성을 기억한다.

목적:

- 난방 비수기 후에도 학습 유지
- 다음 겨울 시작 시 Cold Start 제거
- Current 데이터가 부족할 때 baseline 제공

Long-term Curve는 Raw Cycle을 직접 빠르게 흡수하면 안 된다.

Current Curve가 충분한 신뢰도를 얻었을 때만 천천히 업데이트한다.

예:

```text
new_long_term
=
old_long_term × 0.98
+
current_curve × 0.02
```

단, 실제 alpha 값은 구현 상수로 분리한다.

예:

```text
LONG_TERM_ALPHA = 0.02
```

Long-term 업데이트 조건:

```text
Current confidence >= minimum threshold
AND
Current sample count >= minimum sample threshold
AND
Current curve is stable
```

---

# 16. Current / Long-term 혼합

실제 예측에서는 Current와 Long-term을 confidence에 따라 혼합한다.

```text
Prediction Curve
=
Long-term × (1 - W)
+
Current × W
```

여기서 W는 Current Curve의 confidence에서 계산한다.

예:

```text
Current 데이터 없음
→ W = 0
→ Long-term 100%

Current 학습 초기
→ W = 0.2 ~ 0.5

Current 충분
→ W = 0.8 ~ 1.0
```

정확한 mapping 함수는 추후 조정 가능하다.

---

# 17. Bucket 통계

각 Current / Long-term bucket은 최소 다음 정보를 가진다.

```text
mean_delta_c
sample_count
variance 또는 M2
updated_at
```

권장:

```text
curve_type
bucket_index
mean_delta_c
sample_count
variance
updated_at
```

현재 구현의 `mean + sample_count`만으로는 신뢰도를 충분히 계산할 수 없다.

분산 정보를 추가한다.

---

# 18. Confidence

Confidence는 전역 하나가 아니라 **Curve별**로 관리한다.

예:

```text
Cold Current Confidence
Warm Current Confidence
Predictive Warm Current Confidence
Cooling Current Confidence
```

Long-term도 동일하다.

Confidence는 최소 다음 요소를 고려한다.

```text
sample count
variance
최근 업데이트 시점
연속적인 bucket coverage
```

예:

```text
샘플 많음
+ 분산 작음
+ 최근 데이터
+ 필요한 시간 범위 bucket이 충분함
→ 높은 confidence
```

---

# 19. Predictive Warm Fallback

Predictive Warm은 Balanced/Comfort 운전이 충분히 축적되기 전까지 데이터가 부족할 수 있다.

Fallback 순서:

```text
Predictive Warm Current
        ↓ 부족
Predictive Warm Long-term
        ↓ 부족
Warm Current
        ↓ 부족
Warm Long-term
        ↓ 부족
Legacy safe controller
```

Cold도 Current 데이터가 부족하면:

```text
Cold Long-term
```

을 우선 사용한다.

---

# 20. Outlier와 환경 변화 구분

현재 구현처럼 기존 Curve와 다르다는 이유만으로 바로 reject하면 안 된다.

다음 두 현상을 구분해야 한다.

### Outlier

예:

- 창문 개방
- 센서 오류
- 갑작스러운 외부 열원
- HA 재시작
- 수동 Heater 조작
- 비정상 온도 jump

### Regime Change

예:

- 한파 시작
- 계절 변화
- 며칠간 지속되는 새로운 냉각 패턴

새 사이클이 Current Curve와 크게 다를 경우:

```text
1회 발생
→ anomaly candidate

같은 방향으로 반복
→ 실제 환경 변화 가능성 증가
→ Current Curve가 적응하도록 허용
```

즉:

```text
difference from current curve
≠ automatic reject
```

이다.

물리적으로 불가능하거나 명확한 센서 오류만 즉시 reject한다.

---

# 21. Incomplete / Invalid Cycle

다음 경우 학습 표준에 반영하지 않는다.

```text
sensor unavailable
heater unavailable
manual heater override
HA restart/reload
manual target change during cycle
manual HVAC mode change during cycle
AWAY cycle
impossible temperature jump
incomplete peak observation
communication failure
```

단, Raw Cycle metadata는 디버깅을 위해 저장할 수 있다.

```text
accepted = false
quality_reason = ...
```

---

# 22. Restart 처리

HA 재시작 전후의 사이클을 이어 붙이지 않는다.

```text
restart
→ active cycle incomplete
→ standard curve update 금지
```

이미 완결되어 저장된:

```text
Current Curve
Long-term Curve
Raw completed cycle
```

는 복원한다.

---

# 23. Predictive OFF

Predictive OFF의 목표는 다음 질문에 답하는 것이다.

> 지금 보일러를 끄면 잔열까지 포함한 최종 최고온도가 몇 도가 될 것인가?

최종 구현에서는 단순 scalar `residual_rise`만 사용하지 않고 학습된 Curve를 활용해야 한다.

현재 상태:

```text
Current heating elapsed time
Current temperature
Active Heating Curve
```

에서 미래 Heating/Coasting trajectory를 계산한다.

개념:

```text
현재온도
+
학습 Curve에서 예상되는 나머지 온도 상승
=
Predicted Peak
```

판단:

```text
Predicted Peak >= desired upper target
→ Heater OFF
```

단:

```text
minimum ON time
anti-short-cycle
safety rules
```

를 항상 우선한다.

---

# 24. Predictive ON

Predictive ON의 목표는 다음 질문에 답하는 것이다.

> 지금 난방을 켜지 않으면 난방 반응이 시작되기 전에 실내온도가 허용 범위 아래로 떨어지는가?

최종적으로는 Cooling Curve와 Heating Curve를 함께 사용한다.

```text
현재 Cooling 상태
      ↓
미래 실내온도 예상
      ↓
난방 ON
      ↓
Heating response delay
      ↓
온도 상승 시작
```

따라서:

```text
Cooling prediction
+
Heating response prediction
```

을 조합해야 한다.

현재 단순 slope 기반 Predictive ON은 fallback으로 유지할 수 있다.

---

# 25. Eco / Balanced / Comfort

사용자에게 제공되는 운전 정책 셀렉터는 유지한다.

```text
Eco
Balanced
Comfort
```

한국어:

```text
절약
균형
쾌적
```

### Eco

```text
Predictive ON 사용 안 함
Predictive OFF 사용
```

실제 lower threshold에 도달한 뒤 난방을 시작한다.

가장 많은 `WARM_HEATING` 데이터를 만든다.

### Balanced

Predictive ON을 보수적으로 사용한다.

목표는:

```text
쾌적성과 사이클 수 절충
```

### Comfort

Predictive ON을 적극적으로 사용한다.

목표는:

```text
목표온도 아래로 떨어지는 시간을 최소화
```

Balanced / Comfort는 주로:

```text
PREDICTIVE_WARM_HEATING
```

데이터를 만든다.

---

# 26. Learning Model Selector

현재 개발 버전에는:

```text
existing
curve
```

두 가지 학습 모델 선택기가 존재한다.

이는 개발/검증 단계에서는 유지 가능하다.

```text
기존 학습
커브 학습
```

하지만 최종 제품에서는 제거하는 것을 목표로 한다.

최종 구조:

```text
Curve Model
   ↓
confidence 충분
→ Curve Prediction

confidence 부족
→ Legacy Safe Fallback
```

즉 사용자가 학습 알고리즘을 직접 선택할 필요가 없도록 한다.

---

# 27. 기존 Scalar Learning의 역할

기존 다음 값들은 폐기할 필요는 없다.

```text
response_delay
heating_rate
residual_rise
peak_delay
```

다만 최종적으로는 독립적인 주 학습모델이 아니라 **Curve에서 계산되는 파생 진단값**으로 본다.

예:

```text
response_delay
= Heating Curve에서 최초 의미 있는 상승 bucket

residual_rise
= OFF 온도 대비 Peak 상승량

peak_delay
= OFF → Peak 시간

heating_rate
= Curve 일부 구간의 평균 slope
```

Legacy scalar 모델은:

```text
cold start
insufficient curve data
storage failure
```

시 fallback으로 유지할 수 있다.

---

# 28. 센서/진단 엔티티

현재 기존 scalar 중심 센서는 유지할 수 있지만, Curve 중심 진단 엔티티를 추가한다.

핵심 권장 센서:

```text
active_curve
curve_phase

current_curve_confidence
long_term_curve_confidence
current_curve_sample_count

predicted_peak_temperature

observed_cycles
accepted_learning_cycles
rejected_learning_cycles

last_curve_quality
```

선택적 진단:

```text
response_delay
residual_rise
peak_delay
heating_rate
heat_loss_rate
```

Curve confidence는 전역 하나가 아니라 curve별 값을 내부적으로 유지해야 한다.

UI에서는 active curve에 해당하는 confidence를 우선 표시할 수 있다.

---

# 29. Curve Phase

런타임에서 현재 열상태를 명확히 구분한다.

```text
IDLE
HEATING
COASTING
COOLING
```

의미:

```text
HEATING
→ Heater ON

COASTING
→ Heater OFF 이후 Peak 이전

COOLING
→ Peak 이후

IDLE
→ 유효한 사이클 관측 중이 아님
```

---

# 30. SQLite 권장 구조

기존 SQLite 구조를 확장한다.

## cycles

```text
id
curve_type
start_reason
off_reason
end_reason
mode
preset

started_at
off_at
peak_at
ended_at

accepted
quality_reason
```

## cycle_buckets

```text
cycle_id
curve_type
bucket_index
delta_c
```

최근 7일 Raw 저장.

## current_curve_buckets

```text
curve_type
bucket_index
mean_delta_c
variance
sample_count
updated_at
```

## long_term_curve_buckets

```text
curve_type
bucket_index
mean_delta_c
variance
sample_count
updated_at
```

필요하면 feature table:

```text
curve_type
response_delay
residual_rise
peak_delay
confidence
updated_at
```

를 Current / Long-term별로 분리할 수 있다.

---

# 31. 데이터 업데이트 흐름

```text
Heating Cycle Completed
        ↓
Quality Check
        ↓
Valid?
 ┌──────┴──────┐
 No            Yes
 │              │
Raw metadata    Raw 5-min buckets
only            저장
                ↓
          Current EWMA update
                ↓
       Current confidence update
                ↓
     충분히 안정적이고 신뢰도 높음?
           ┌────┴────┐
           No        Yes
                     ↓
              Long-term slow update
```

---

# 32. 예측 모델 선택

실제 사용할 Prediction Curve:

```text
if Current confidence high:
    Current 중심

elif Long-term exists:
    Current + Long-term blend

elif fallback curve exists:
    related curve fallback

else:
    legacy thermostat/TPI
```

Predictive Warm 예:

```text
Predictive Current
→ Predictive Long-term
→ Warm Current
→ Warm Long-term
→ Legacy
```

---

# 33. Fail-safe 우선순위

제어 순서는 항상 다음과 같다.

```text
1. Safety
2. Sensor validity
3. Heater availability
4. Minimum ON/OFF
5. Anti-short-cycle
6. Predictive Curve Controller
7. Legacy fallback
8. Basic hysteresis
```

어떤 학습 결과도 Safety나 minimum ON/OFF를 무시할 수 없다.

---

# 34. MVP에서 제외할 항목

다음은 이후 연구 대상으로 둔다.

```text
Away Cooling Curve
Passive Solar Heat Profile
sun elevation / azimuth compensation
weather/cloud compensation
monthly gas consumption calibration
actual heat-loss W/K estimation
occupancy internal heat model
```

현재 커브 시스템이 실제 난방 환경에서 충분히 검증된 후 고려한다.

---

# 35. 현재 코드에서 필요한 주요 변경사항

현재 `adapted_climate` 코드 기준 주요 변경은 다음과 같다.

```text
COLD_OFF_SECONDS
3600 → 10800
```

Cold 분류 우선순위를 변경한다.

AWAY 상태는 학습에서 제외한다.

AWAY → HOME 이후 첫 난방을 Cold로 분류한다.

현재 영속 `curve_buckets`의 의미를:

```text
Current Curve
```

로 재정의한다.

추가로:

```text
Long-term Curve
```

저장 계층을 만든다.

각 bucket에 variance를 추가한다.

Curve별 confidence를 계산한다.

Outlier와 지속적인 regime change를 구분한다.

Predictive OFF가 scalar residual만 사용하는 방식에서 발전하여 실제 Heating + Coasting Curve를 예측에 사용하도록 한다.

Predictive ON이 단일 cooling slope만 사용하는 방식에서 발전하여 Cooling Curve + Heating Response를 사용할 수 있도록 한다.

기존 scalar learning은 fallback 및 diagnostic으로 유지한다.

---

# 36. 핵심 설계 원칙

이 프로젝트의 가장 중요한 원칙은 다음 한 문장으로 정의한다.

> Adaptive Floor Heating은 몇 개의 고정 열상수만 학습하는 온도조절기가 아니라, 실제 주택에서 관측된 난방 사이클의 시간-온도 궤적을 학습하고 현재 상태에 맞는 미래 궤적을 예측하여 난방을 제어하는 시스템이다.

그리고 메모리 구조는 다음으로 고정한다.

```text
Raw Cycle
    ↓
Current EWMA Curve
    ↓
Long-term Curve
    ↓
Confidence-aware Prediction
```

운전 정책은:

```text
Eco
Balanced
Comfort
```

이며 학습 데이터는 운전 정책이 아니라 실제 열상태를 기준으로:

```text
Cold
Warm
Predictive Warm
Cooling
```

으로 분류한다.


좋습니다. 현재 `dev` 코드와 방금 만든 명세를 기준으로 바로 구현 체크리스트로 정리하면 아래처럼 가면 됩니다.

# Adaptive Floor Heating
## 구현 변경 체크리스트

### 1. Cold / Warm / Predictive Warm 분류

- [ ] `COLD_OFF_SECONDS = 3600`을 `10800`으로 변경
- [ ] Cold 판정이 Predictive Start보다 우선하도록 분류 순서 변경
- [ ] `AWAY` 상태에서는 새 Curve 학습 사이클을 표준 모델에 반영하지 않도록 처리
- [ ] `AWAY → HOME` 전환 후 첫 난방을 `COLD_HEATING`으로 강제 분류
- [ ] 첫 Cold 난방이 시작되거나 완료된 뒤 `away_return_pending` 상태 해제
- [ ] `start_reason`과 `curve_type`을 서로 혼동하지 않도록 분리 유지
- [ ] 테스트 추가:
  - HOME + OFF 2시간 → Warm
  - HOME + OFF 3시간 이상 → Cold
  - HOME + OFF 4시간 + Predictive Start → Cold
  - AWAY 중 난방 → 학습 제외
  - AWAY → HOME 첫 난방 → Cold
  - HOME 복귀 두 번째 정상 난방 → Warm/Predictive Warm 정상 분류

---

### 2. Away 학습 제외

- [ ] `CurveTracker` 또는 Runtime에 현재 preset 전달
- [ ] AWAY 시작 시 진행 중 HOME cycle은 invalid 처리
- [ ] AWAY 상태에서 생성된 Heating/Coasting/Cooling 결과는 Current/Long-term 갱신 금지
- [ ] 필요하면 Raw metadata에는 `preset=away`로 기록
- [ ] AWAY 데이터가 accepted cycle 카운터에 포함되지 않도록 확인
- [ ] AWAY 종료 시 기존 AWAY cycle을 HOME cycle과 이어붙이지 않음

---

### 3. Current Curve 재정의

현재 `curve_buckets`는 영속 EWMA 표준이다.

이를 개념적으로 `Current Curve`로 변경한다.

- [ ] 현재 `curve_buckets`를 Current Curve로 재정의
- [ ] 코드 클래스/주석/문서에서 `standard` 용어를 `current curve`로 정리
- [ ] 현재 EWMA:
  ```text
  current_new = current_old × 0.8 + cycle × 0.2
  ```
  유지
- [ ] `CURRENT_ALPHA = 0.2` 상수화
- [ ] Raw 7일 retention은 그대로 유지
- [ ] Current Curve는 HA 재시작 후 SQLite에서 복원
- [ ] Current Curve는 난방 비수기에도 삭제하지 않음
- [ ] Current Curve의 `updated_at` 관리

---

### 4. Long-term Curve 추가

- [ ] SQLite에 `long_term_curve_buckets` 추가
- [ ] 각 bucket 저장 필드:
  ```text
  curve_type
  bucket_index
  mean_delta_c
  variance/M2
  sample_count
  updated_at
  ```
- [ ] Long-term Curve를 메모리에 로드하는 모델 추가
- [ ] Long-term은 Raw cycle을 직접 빠르게 반영하지 않음
- [ ] Current Curve가 충분히 안정적일 때만 Long-term 업데이트
- [ ] 초기 제안:
  ```text
  LONG_TERM_ALPHA = 0.02
  ```
- [ ] 실제 alpha는 상수로 분리
- [ ] 난방 시즌이 끝나도 Long-term 유지
- [ ] 다음 난방 시즌 시작 시 Long-term을 즉시 prediction source로 사용

---

### 5. Current → Long-term 승격 조건

- [ ] Current Curve에 최소 sample threshold 정의
- [ ] Current variance threshold 정의
- [ ] 필요한 bucket coverage 조건 정의
- [ ] 최근 업데이트 조건 정의
- [ ] 조건을 만족할 때만 Long-term 업데이트
- [ ] 단일 사이클 결과가 Long-term에 직접 큰 영향을 주지 않도록 보장
- [ ] Long-term 업데이트 횟수/마지막 갱신시각 진단 가능하게 구성

---

### 6. Variance 추가

현재는 `mean + sample_count`만 저장한다.

- [ ] Current bucket에 variance 또는 Welford M2 추가
- [ ] Long-term bucket에도 동일 추가
- [ ] DB schema version 증가
- [ ] 기존 DB migration 구현
- [ ] migration 실패 시 학습 DB 전체 삭제 대신 안전한 fallback 검토
- [ ] variance가 0/미정인 초기 bucket 처리
- [ ] 테스트:
  - 첫 sample
  - 두 번째 sample
  - zero delta sample
  - 큰 편차 sample
  - migration

---

### 7. Curve별 Confidence

- [ ] 전역 confidence 하나만 사용하지 않도록 설계
- [ ] 최소 다음 confidence를 내부 관리:
  ```text
  COLD_CURRENT
  COLD_LONG_TERM
  WARM_CURRENT
  WARM_LONG_TERM
  PREDICTIVE_WARM_CURRENT
  PREDICTIVE_WARM_LONG_TERM
  COOLING_CURRENT
  COOLING_LONG_TERM
  ```
- [ ] confidence 구성 요소:
  - sample_count
  - variance
  - bucket coverage
  - recency
- [ ] 0~1 범위로 정규화
- [ ] confidence 계산식은 별도 함수로 분리
- [ ] confidence 부족 시 fallback 경로 테스트

---

### 8. Prediction Curve 선택/혼합

- [ ] Current와 Long-term을 혼합하는 API 추가
- [ ] 개념:
  ```text
  prediction =
      long_term × (1 - weight)
      + current × weight
  ```
- [ ] weight는 Current confidence 기반
- [ ] Current 없음 → Long-term 100%
- [ ] Long-term 없음 → Current 사용
- [ ] 둘 다 없음 → fallback
- [ ] bucket별 데이터가 한쪽에만 있을 때 처리 규칙 정의
- [ ] 예상 trajectory를 반환하는 공통 API 추가

예:

```text
predict_heating_curve(curve_type)
predict_cooling_curve()
```

---

### 9. Predictive Warm fallback

- [ ] fallback 순서 구현:
  ```text
  Predictive Warm Current
  → Predictive Warm Long-term
  → Warm Current
  → Warm Long-term
  → Legacy
  ```
- [ ] response delay도 동일 fallback 규칙 사용
- [ ] residual/coast prediction도 동일 규칙 사용
- [ ] active curve diagnostics에 실제 사용된 source 표시

예:

```text
PREDICTIVE_WARM_CURRENT
WARM_LONG_TERM_FALLBACK
LEGACY_FALLBACK
```

---

### 10. Outlier 처리 재설계

현재 `CURVE_DEVIATION`은 새 환경 적응을 막을 수 있다.

- [ ] 물리적으로 불가능한 sample과 기존 Curve와 다른 sample을 구분
- [ ] 즉시 reject 대상:
  - sensor unavailable
  - impossible jump
  - manual override
  - HA restart
  - incomplete cycle
  - invalid actuator state
- [ ] Curve deviation은 즉시 영구 reject하지 않고 anomaly 후보로 관리
- [ ] 비슷한 방향의 deviation이 반복되면 regime change로 판단 가능하게 설계
- [ ] repeated deviation이 Current Curve에 반영되도록 구현
- [ ] Long-term은 regime change에 느리게 반응
- [ ] anomaly streak 또는 candidate buffer 필요 여부 결정
- [ ] 테스트:
  - 단일 이상치
  - 연속 3개 동일 방향 변화
  - 정상 복귀
  - 센서 오류와 regime change 구분

---

### 11. Peak 검출

현재 방식은 유지 가능.

- [x] OFF 후 최고온도 candidate 추적
- [x] 더 높은 온도 발생 시 candidate 갱신
- [x] 10분 동안 더 높은 온도 없으면 Peak 확정

추가 변경:

- [ ] `PEAK_SETTLE_SECONDS` 의미 문서화
- [ ] `MAX_AFTER_OFF_SECONDS` 분리
- [ ] `MAX_PEAK_WAIT`
- [ ] `MAX_COOLING_OBSERVATION`
- [ ] 둘 다 초기값 3시간 가능
- [ ] 한 상수 변경이 다른 기능에 의도치 않게 영향 주지 않도록 분리

---

### 12. Cooling Curve

- [x] Peak 이후 별도 Cooling Curve 생성
- [x] 5분 bucket 사용
- [x] 다음 ON에서 종료 가능
- [ ] Current Cooling Curve 추가
- [ ] Long-term Cooling Curve 추가
- [ ] Cooling Curve variance/confidence 추가
- [ ] Cooling prediction API 추가
- [ ] Cooling Curve가 실제 Predictive ON 계산에 사용되도록 연결

---

### 13. Predictive OFF 개선

현재 구현:

```text
active shape match
→ scalar residual_rise
→ current temp + residual
```

목표 구현:

```text
현재 elapsed 위치
→ Prediction Heating/Coasting Curve
→ 남은 미래 ΔT 합산
→ Predicted Peak
```

체크리스트:

- [ ] 현재 cycle의 elapsed bucket 계산
- [ ] 현재까지 관측 curve와 prediction source alignment
- [ ] 남은 Heating/Coasting trajectory 계산
- [ ] 예상 Peak 계산
- [ ] `predicted_peak_temperature` 생성
- [ ] target upper boundary와 비교
- [ ] minimum ON time 우선
- [ ] shape mismatch 시 fallback
- [ ] insufficient curve data 시 legacy residual fallback

---

### 14. Predictive ON 개선

현재 구현:

```text
현재 cooling slope
× response delay
```

목표:

```text
Cooling Curve
+
Heating response curve
```

체크리스트:

- [ ] 현재 Cooling phase elapsed 추정
- [ ] Cooling Prediction Curve에서 미래 하강 궤적 계산
- [ ] candidate ON 시점별 미래 실내온도 계산
- [ ] Heating response delay/초기 상승 Curve 결합
- [ ] lower comfort boundary 침범 전 필요한 ON 시점 계산
- [ ] Eco에서는 Predictive ON 금지
- [ ] Balanced는 보수적 margin
- [ ] Comfort는 적극적 margin
- [ ] Cooling curve 부족 시 기존 slope 방식 fallback

---

### 15. Eco / Balanced / Comfort

- [x] Select 엔티티 존재
- [x] `eco / balanced / comfort`
- [ ] 각 모드 의미를 명세대로 고정
- [ ] Eco:
  - Predictive ON 없음
  - Predictive OFF 있음
- [ ] Balanced:
  - Predictive ON 보수적
  - Predictive OFF 있음
- [ ] Comfort:
  - Predictive ON 적극적
  - Predictive OFF 있음
- [ ] 모드 변경이 학습 Curve를 초기화하지 않도록 확인
- [ ] mode 자체보다 실제 `start_reason`으로 Curve 분류

---

### 16. Learning Model Selector

현재:

```text
existing
curve
```

- [x] 개발용 셀렉터 존재
- [ ] 개발 단계에서는 유지
- [ ] Curve 모델 검증 완료 후 제거 후보
- [ ] 최종적으로 automatic fallback 구조로 변경
- [ ] 사용자에게 알고리즘 선택 책임을 넘기지 않도록 설계
- [ ] 기존 모델을 내부 fallback으로 유지

---

### 17. Legacy scalar 모델 정리

- [ ] `response_delay`
- [ ] `heating_rate`
- [ ] `residual_rise`
- [ ] `peak_delay`

위 항목은 장기적으로 Curve derived feature로 재정의한다.

- [ ] 기존 scalar EWMA는 fallback으로 유지
- [ ] Curve confidence 충분 시 scalar primary control 비활성화
- [ ] 진단 센서 값이 legacy인지 curve-derived인지 구분
- [ ] 문서에서 중복 모델 설명 정리

---

### 18. Diagnostic Sensor 재편

현재 센서는 legacy 중심이다.

추가 권장:

- [ ] `active_curve`
- [ ] `curve_phase`
- [ ] `prediction_source`
- [ ] `current_curve_confidence`
- [ ] `long_term_curve_confidence`
- [ ] `current_curve_sample_count`
- [ ] `predicted_peak_temperature`
- [ ] `last_curve_quality`

선택적:

- [ ] current/long-term confidence를 curve type별 attribute로 제공할지 결정
- [ ] 현재 active curve만 엔티티로 노출하고 전체값은 attribute/UI로 제공할지 결정

기존 센서 검토:

- [ ] `learned_heating_rate` 기본 활성 필요성 재검토
- [ ] `learned_response_delay` curve-derived 전환
- [ ] `learned_residual_rise` curve-derived 전환
- [ ] `learned_peak_delay` curve-derived 전환
- [ ] `TOTAL_INCREASING` 센서가 reset 시 semantic 문제 없는지 검토

---

### 19. Curve Phase

- [ ] 내부 phase 상태 명시:
  ```text
  IDLE
  HEATING
  COASTING
  COOLING
  ```
- [ ] Heater OFF ≠ 항상 Cooling으로 처리하지 않도록 확인
- [ ] OFF 후 Peak 전은 COASTING
- [ ] Peak 확정 이후 COOLING
- [ ] phase를 진단 엔티티로 제공

---

### 20. SQLite Schema

- [ ] schema version 2 이상으로 증가
- [ ] `cycles`에 `preset` 필드 추가
- [ ] `current_curve_buckets`
- [ ] `long_term_curve_buckets`
- [ ] variance/M2 저장
- [ ] curve confidence 관련 metadata 필요 여부 결정
- [ ] migration 테스트
- [ ] duplicate cycle 방지 유지
- [ ] WAL / worker thread 구조 유지
- [ ] 7일 raw cleanup 유지

---

### 21. Safety / Fail-safe

다음 기존 원칙 유지:

- [x] sensor unavailable → heater OFF
- [x] heater unavailable → fault
- [x] minimum ON
- [x] minimum OFF
- [x] startup OFF 확인
- [x] external override 감지
- [x] restart 중 active cycle discard

추가:

- [ ] Curve model failure가 actuator safety에 영향 주지 않도록 확인
- [ ] SQLite failure → legacy fallback
- [ ] Long-term corruption → Current 또는 legacy fallback
- [ ] confidence 계산 실패 → legacy fallback
- [ ] prediction exception → 기본 thermostat decision 유지

---

### 22. 테스트 우선순위

#### Phase A — 분류 및 Away
- [ ] Cold 3시간
- [ ] Away 학습 제외
- [ ] Away → Home first Cold
- [ ] Predictive start + Cold precedence

#### Phase B — Current/Long-term storage
- [ ] Current EWMA
- [ ] Long-term slow update
- [ ] restart restore
- [ ] schema migration
- [ ] raw cleanup

#### Phase C — Confidence/variance
- [ ] variance
- [ ] sample coverage
- [ ] confidence
- [ ] fallback

#### Phase D — Prediction
- [ ] Heating trajectory
- [ ] predicted peak
- [ ] Cooling trajectory
- [ ] predictive ON
- [ ] scalar fallback

#### Phase E — Integration
- [ ] Eco
- [ ] Balanced
- [ ] Comfort
- [ ] existing/curve development selector
- [ ] actual HA runtime test

---

# 구현 우선순위

## P0 — 개념 오류 수정

- [ ] Cold 3시간
- [ ] Away 학습 제외
- [ ] Away → Home first Cold
- [ ] Cold classification precedence
- [ ] MAX_PEAK_WAIT / MAX_COOLING 분리

## P1 — 메모리 구조

- [ ] Current Curve 명확화
- [ ] Long-term Curve 추가
- [ ] variance 추가
- [ ] confidence 추가
- [ ] Current/Long-term prediction blending

## P2 — 실제 Curve 제어 연결

- [ ] Heating Curve 기반 Predictive OFF
- [ ] Cooling Curve 기반 Predictive ON
- [ ] prediction source fallback 체계

## P3 — 진단/UI

- [ ] active curve
- [ ] phase
- [ ] confidence
- [ ] predicted peak
- [ ] quality result
- [ ] custom graph UI

## P4 — 정리

- [ ] legacy scalar 역할 축소
- [ ] learning model selector 제거 여부 결정
- [ ] 문서/README 최신화
- [ ] 실제 겨울 난방 field calibration


