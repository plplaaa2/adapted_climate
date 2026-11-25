# Project File Tree

```text
adaptive_floor_heating_climate/
├── .gitattributes
├── .gitignore
├── CHANGELOG.md
├── README.md
├── README.en.md
├── adaptive_floor_heating_climate_spec.md
├── brand/
│   └── icon.png
├── caution.jsonl
├── changelog.jsonl
├── changelog_1.jsonl
├── changelog_2.jsonl
├── hacs.json
├── requirements-test.txt
├── tree.md
├── docs/
│   ├── README.md
│   ├── README.en.md
│   ├── setup.md
│   ├── setup.en.md
│   ├── operation.md
│   ├── operation.en.md
│   ├── maintenance.md
│   ├── maintenance.en.md
│   ├── troubleshooting.md
│   └── troubleshooting.en.md
├── custom_components/
│   └── adaptive_floor_heating/
│       ├── __init__.py
│       ├── actuator.py
│       ├── brand/
│       │   └── icon.png
│       ├── binary_sensor.py
│       ├── button.py
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
│       ├── frontend/
│       │   ├── panel.en.js
│       │   └── panel.js
│       ├── panel.py
│       ├── peak_prediction.py
│       ├── peak_migration.py
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
│       ├── time.py
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
            ├── test_history.py
            ├── test_history_frontend.cjs
            ├── test_learning_frontend.cjs
            ├── test_off_response.py
            ├── test_panel.py
            ├── test_peak_prediction.py
            ├── test_peak_migration.py
            ├── test_panel_frontend.cjs
            ├── test_sensor_frontend.cjs
            ├── test_runtime.py
            └── test_thermal_model.py
```

Standalone and individual-room Climate entries expose OFF/HEAT/AUTO plus HOME/AWAY presets; an integrated multi-room entry controls 2–32 switches through one shared sensor, Climate, and learning model. The predictive ON Switch replaces the retired policy Select while preserving eco=OFF and balanced/comfort=ON storage compatibility. Both control paths use full measured response lag and continuously covered Cooling at the lower hysteresis threshold. A Number entity adjusts the peak observation limit (six-hour default, one to twelve hours). The current heating information card reports planned runtime and a single scheduled cutoff. Confirmed OFF Peak appears as a horizontal reference, with no invented arrival time. Confirmation freezes a contiguous learned Cooling trajectory anchored to measured Peak and persists it independently of active observations. All group switches must confirm the same state, with mixed/unavailable states handled by group OFF and a fault lock. Climate setpoint commands use a configurable confirmation delay, defaulting to 60 seconds. Indoor/outdoor temperature and selected weather inputs share startup and five-minute snapshots across control, overheating protection and learning. Intent, basic learning and curve memory use separate stores. Legacy Auto Control switch state migrates into AUTO.

README files provide installation and first-run steps. The bilingual docs/ guide is the maintained source for setup, operation/learning, maintenance and troubleshooting; setup and operation describe Climate command confirmation delays and shared five-minute sensor checks. No separate hosted Wiki is published.

The sensor catalog exposes three phase-only entities: curve learning, temperature observation, and prediction. The unused initial-learning placeholder and its UI/translations are removed; setup retires its owned registry entry while preserving the remaining sensor identities. Learning continues through normal heating cycles without a separate initialization procedure. Numeric observations and forecast comparisons appear in Climate attributes and the panel. Confirmed OFF diagnostics, frozen Cooling predictions and completed comparisons retain wall-clock metadata across restarts without resuming incomplete observations. Outdoor input supports outdoor-conditioned runtime forecasts and additive Peak correction. Heat-loss observation, learning and experimental forecasts are removed; legacy heat-loss fields round-trip only for storage compatibility. Retired supply/return options are ignored and removed on options save. Retired pipe and experimental sensor IDs remain for registry cleanup. Entity prefixes use `adaptive_heating_climate` or numbered `adaptive_heating_climate_room_n`.

Windows tests cover history, both learning models, diagnostics, storage compatibility, controller, actuator, flow, standalone/grouped runtime and virtual command dispatch using HA boundary doubles and real voluptuous. Live HA registration, presentation and physical accuracy validation remain pending. The ignored .venv, .git and external backup snapshots are omitted.

The administrator-only Migration button in Learning Analysis runs historical recovery for the selected Climate. Peak recovery replays retained Recorder sensor states within the original observation intervals, preserves original rows and buckets as audit evidence, and updates learning in a SQLite transaction. Sunrise saving recovery uses the configured window to reassess retained heating and cooling cycles with independent completion markers. Repeat runs cannot admit a recovered row twice. Startup still opens and upgrades the storage schema and applies raw retention.

The Learning Analysis tab provides four curve summaries, filtered recent cycle records, measured timeline/quality evidence, committed Current before/after changes and Long-term promotion outcomes, raw bucket charts/tables, and the original learned-curve comparison. The authenticated read-only curve_cycles API queries schema-8 SQLite metadata without learning, prediction or writes. Nullable legacy evidence and expired seven-day raw buckets remain distinct. Diagnostic metadata commits with cycle/learning changes and survives raw cleanup. Both observers now confirm a measured 0.2 C fall followed by another 0.1 C fall on the next report, with a shared four-hour OFF horizon across quality checks, profiles, memory prediction and restore. Saved evidence exposes these thresholds and measured confirmation values; other quality and control safety gates remain unchanged.

Prediction Analysis places current forecast highlights above its graph, with expandable calculation and validation evidence. Its final card reads exactly five recent heating rounds from the existing curve_cycles API using heating_only, independently of Recorder. Matching original forecasts are frozen in each cycle's analysis evidence; missing legacy predictions stay unknown. The separate prediction_accuracy SQLite table accumulates the all-time count and total absolute error transactionally with unique cycle inserts. The displayed MAE uses all stored comparisons, survives raw TTL/restarts, and starts with the introduction of this recording feature rather than reconstructing old forecasts.

Schema 17 stores runtime-average admission independently of cycle retention. The global latest-ten eligible mean excludes runs shorter than 60% of the evidence available before their ON edge without removing forecast samples. Predictive OFF relaxes measured runtime support by up to 20% before ten accepted heating cycles, then 10% plus a separately earned outdoor/bias allowance up to 5%, retaining the mean floor, configured minimum ON, shortfall and hybrid evidence guards.
