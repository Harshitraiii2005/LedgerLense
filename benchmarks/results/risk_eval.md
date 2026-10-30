# Risk rules vs synthetic labels — 26871 scored txns
Run at 2026-10-30T14:26:45.749158+00:00. Command: `MONGO_URI=... uv run python benchmarks/risk_eval.py`

precision **0.4065**, recall **0.5612** (tp=509, fp=743, fn=398, tn=25221)

| pattern | recall |
|---|---|
| amount_spike | 0.6559 |
| impossible_geography | 0.4759 |
| velocity_burst | 0.5694 |

False positives by reason: {"COUNTRY_JUMP": 432, "VELOCITY": 301, "AMOUNT_ZSCORE+COUNTRY_JUMP": 1, "AMOUNT_ZSCORE": 9}
