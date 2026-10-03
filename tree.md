# Project File Tree

```text
adaptive_floor_heating_climate/
├── .gitattributes
├── .gitignore
├── README.md
├── adaptive_floor_heating_climate_spec.md
├── brand/
│   └── icon.png
├── caution.jsonl
├── changelog.jsonl
├── hacs.json
├── requirements-test.txt
├── tree.md
├── docs/
│   ├── model_operation.md
│   ├── heating_learning_and_auto_control.md
│   ├── curve_learning.md
│   ├── sensor_layout.md
│   ├── phase_1_spec.md
│   ├── phase_2_spec.md
│   ├── phase_3_spec.md
│   ├── phase_4_spec.md
│   └── system_architecture.md
├── custom_components/
│   └── adaptive_floor_heating/
│       ├── __init__.py
│       ├── actuator.py
│       ├── brand/
│       │   └── icon.png
│       ├── binary_sensor.py
│       ├── climate.py
│       ├── config_flow.py
│       ├── const.py
│       ├── controller.py
│       ├── curve_learning.py
│       ├── curve_memory.py
│       ├── curve_storage.py
│       ├── diagnostics.py
│       ├── entity_naming.py
│       ├── experimental.py
│       ├── coordinator.py
│       ├── history.py
│       ├── manifest.json
│       ├── off_response.py
│       ├── runtime.py
│       ├── select.py
│       ├── sensor.py
│       ├── storage.py
│       ├── thermal_model.py
│       ├── water_observation.py
│       └── translations/
│           ├── en.json
│           └── ko.json
└── tests/
    ├── __init__.py
    └── components/
        └── adaptive_floor_heating/
            ├── __init__.py
            ├── conftest.py
            ├── test_actuator.py
            ├── test_config_flow.py
            ├── test_controller.py
            ├── test_curve_learning.py
            ├── test_curve_memory.py
            ├── test_curve_storage.py
            ├── test_diagnostics.py
            ├── test_experimental.py
            ├── test_history.py
            ├── test_off_response.py
            ├── test_runtime.py
            ├── test_thermal_model.py
            └── test_water_observation.py
```

Standalone and individual-room Climate entries expose OFF/HEAT/AUTO plus HOME/AWAY presets; an integrated multi-room entry controls 2–32 switches through one shared sensor, Climate, and learning model. Select entities choose the learning model and eco/balanced/comfort prediction behavior. Existing learning and condition-matched Current/Long-term curve memories observe delayed OFF responses and confirm Peak using sustained measured cooling. Only the selected control path dispatches heater commands; insufficient curve data falls back to basic learning and then hysteresis. All group switches must confirm the same state, with mixed/unavailable states handled by group OFF and a fault lock. Intent, basic learning and curve memory use separate stores. Legacy Auto Control switch state migrates into AUTO.

The sensor catalog has 42 entities: 15 core diagnostics enabled by default, 8 detailed diagnostics, 7 outdoor experiments and 12 pipe experiments disabled by default. Eight new read-only OFF diagnostics expose both predicted peaks, actual Peak, signed model errors, per-prediction confidence and observation/prediction status. Confirmed OFF diagnostics and completed comparisons retain wall-clock cycle metadata across restarts without resuming incomplete observations. Existing entity IDs and registry enablement choices are preserved. `docs/sensor_layout.md` documents graph layout and diagnostic interpretation. Outdoor/supply/return inputs support diagnostic heat-loss learning; experimental forecasts do not control heating. Entity prefixes use `adaptive_heating_climate` or numbered `adaptive_heating_climate_room_n`.

Windows tests cover history, both learning models, diagnostics, storage compatibility, controller, actuator, flow, standalone/grouped runtime and virtual command dispatch using HA boundary doubles and real voluptuous. Live HA registration, presentation and physical accuracy validation remain pending. The ignored .venv, .git and external backup snapshots are omitted.
