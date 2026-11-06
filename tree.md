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
├── changelog_1.jsonl
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
│       ├── curve_api.py
│       ├── curve_learning.py
│       ├── curve_memory.py
│       ├── curve_storage.py
│       ├── diagnostics.py
│       ├── entity_naming.py
│       ├── experimental.py
│       ├── frontend/
│       │   └── panel.js
│       ├── panel.py
│       ├── peak_prediction.py
│       ├── coordinator.py
│       ├── history.py
│       ├── manifest.json
│       ├── number.py
│       ├── off_response.py
│       ├── runtime.py
│       ├── select.py
│       ├── sensor.py
│       ├── storage.py
│       ├── switch.py
│       ├── thermal_model.py
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
            ├── test_curve_api.py
            ├── test_curve_learning.py
            ├── test_curve_memory.py
            ├── test_curve_storage.py
            ├── test_cycle_analysis_frontend.cjs
            ├── test_cycle_frontend.cjs
            ├── test_diagnostics.py
            ├── test_experimental.py
            ├── test_history.py
            ├── test_history_frontend.cjs
            ├── test_learning_frontend.cjs
            ├── test_off_response.py
            ├── test_panel.py
            ├── test_peak_prediction.py
            ├── test_panel_frontend.cjs
            ├── test_sensor_frontend.cjs
            ├── test_runtime.py
            └── test_thermal_model.py
```

Standalone and individual-room Climate entries expose OFF/HEAT/AUTO plus HOME/AWAY presets; an integrated multi-room entry controls 2–32 switches through one shared sensor, Climate, and learning model. The predictive ON Switch replaces the retired policy Select while preserving eco=OFF and balanced/comfort=ON storage compatibility. Both control paths use full measured response lag and continuously covered Cooling at the lower hysteresis threshold. A Number entity adjusts the peak observation limit (six-hour default, one to twelve hours). The current heating information card reports only actual runtime schedule reviews and cumulative extensions; additive Peak has no scheduled review. Confirmed OFF Peak appears as a horizontal reference, with no invented arrival time. Confirmation freezes a contiguous learned Cooling trajectory anchored to measured Peak and persists it independently of active observations. All group switches must confirm the same state, with mixed/unavailable states handled by group OFF and a fault lock. Intent, basic learning and curve memory use separate stores. Legacy Auto Control switch state migrates into AUTO.

The sensor catalog exposes three phase-only entities: curve learning, temperature observation, and prediction. The unused initial-learning placeholder and its UI/translations are removed; setup retires its owned registry entry while preserving the remaining sensor identities. Learning continues through normal heating cycles without a separate initialization procedure. Numeric observations and forecast comparisons appear in Climate attributes and the panel. Confirmed OFF diagnostics, frozen Cooling predictions and completed comparisons retain wall-clock metadata across restarts without resuming incomplete observations. Only the outdoor input supports indoor/outdoor diagnostic heat-loss learning; retired supply/return options are ignored and removed on options save. Pipe observation is removed; retired pipe sensor IDs remain for registry cleanup. Experimental forecasts do not control heating. Entity prefixes use `adaptive_heating_climate` or numbered `adaptive_heating_climate_room_n`.

Windows tests cover history, both learning models, diagnostics, storage compatibility, controller, actuator, flow, standalone/grouped runtime and virtual command dispatch using HA boundary doubles and real voluptuous. Live HA registration, presentation and physical accuracy validation remain pending. The ignored .venv, .git and external backup snapshots are omitted.

The Learning Analysis tab provides four curve summaries, filtered recent cycle records, measured timeline/quality evidence, committed Current before/after changes and Long-term promotion outcomes, raw bucket charts/tables, and the original learned-curve comparison. The authenticated read-only curve_cycles API queries schema-5 SQLite metadata without learning, prediction or writes. Nullable legacy evidence and expired seven-day raw buckets remain distinct. Diagnostic metadata commits with cycle/learning changes and survives raw cleanup. Both observers now confirm a measured 0.2 C fall followed by another 0.1 C fall on the next report, with a shared four-hour OFF horizon across quality checks, profiles, memory prediction and restore. Saved evidence exposes these thresholds and measured confirmation values; other quality and control safety gates remain unchanged.
