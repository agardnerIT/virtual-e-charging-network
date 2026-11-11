# Loki, optionally

`make up STACK=logs` adds Loki (single-binary, filesystem storage, 6 h retention) and a third
hub logs pipeline that writes to it. It is **not** in the default stack: the default sends logs to
the hub's `debug` exporter, and that is a decision, not an omission (D45, `docs/telemetry.md` §5).

The question it answers, which `make logs` cannot answer as a query:

```
{service_name="vecn-ocpp-cp"} | json | ocpp_transaction_id="42"
```

i.e. every OCPP frame of one session, from the charge point's own copies, joined to the trace by
`trace_id`. The measured cost of having it is in `docs/sizing.md`.
