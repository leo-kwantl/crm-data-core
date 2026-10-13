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
- [ ] Scheduler config API + Scheduled-Jobs page (app, after 015)
- [ ] Unit tests for `pipelines/ingest` normalize reject paths
- [ ] Staging + quarantine tables (`01x_ingest_*` migrations, data owns)
- [ ] CDC/outbox design doc
- [ ] Backfill runner + reconciliation counts vs app
