# Tasks: data-pipeline-init

- [x] Split repo from monorepo (ingestion tests + db mirror + pipelines scaffold)
- [x] Daily snapshot history (013: clv/churn/KPI/training-runs + runner + tests)
- [x] Batch upload ETL (014: jobs/staging/quarantine + idempotency cols + runner + tests)
- [x] Core-table apply of staged ok rows (apply.py + --apply + tests)
- [x] 015 segments/pipeline tables + runner (schedule.py + run_pipeline.py
  + tests; seeds nightly/weekly; all 5 ml CLIs mapped, segments per
  confirmed verbatim argv)
- [x] 016 behavior mode + modeling-upgrade flags (--model/--algorithm/
  --content/--include-user-cf mapped; app to re-mirror 016)
- [x] Thin POST /uploads edge (app SHIPPED: reads 014 only, no new tables)
- [ ] Scheduler config API + Scheduled-Jobs page (app, after 015) — CONFIG
  DONE, FIRING NOT. app `openspec/changes/scheduler-config-and-jobs-page/`
  (`57a938c` + `217224b`) records the config half as shipped (`PUT
  /api/v1/pipelines/manifests/{pipeline}`, admin jobs page + DagGraph,
  per-connector `PUT /connectors/{id}/schedule`) and leaves task 4.1 open:
  NOTHING FIRES THE TICK (no cron entry, no unit, no loop), which is why the
  2026-10-17 01:00 window passed silently and the run had to be hand-invoked
  (`python scripts/run_pipeline.py --pipeline nightly --ml-repo ../crm-cdp-ml`,
  no `--as-of` → `business_today()`); read `max(as_of)` in `pipeline_runs`,
  never wait for the tick. app 1.4 (a cadence edit honoured by a live tick)
  is marked NOT VERIFIED. This line is the cross-repo pointer; the app
  change owns the work.
- [ ] Unit tests for `pipelines/ingest` normalize reject paths
- [ ] Staging + quarantine tables (`01x_ingest_*` migrations, data owns)
- [ ] CDC/outbox design doc
- [ ] Backfill runner + reconciliation counts vs app
