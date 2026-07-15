from portal import active_period_code, _reactive_rows, _reactive_filter_rows

period = active_period_code()
site_id = 1

rows = _reactive_rows(period, site_id)

for f in [
    "all",
    "over_limit",
    "inductive_over",
    "capacitive_over",
    "missing",
    "low_consumption",
    "control",
]:
    filtered = _reactive_filter_rows([dict(r) for r in rows], q="", filter_type=f)
    print(f, len(filtered))
