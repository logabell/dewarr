# Testing on a workstation

- Use `python3 scripts/check.py` for local tests. It serializes runs in this
  checkout, lowers priority, caps concurrency, samples memory, enforces a total
  time budget, and cleans up its process group. Do not bypass it to retry failures.
- Start with relevant files or the small default browser/API journeys. Unit tests
  are for cheap boundary/security/parser cases that broader journeys cannot cover
  well. Do not add tests that only assert implementation strings or duplicate a
  workflow already covered at the API/browser level.
- `npm --prefix apps/web test` runs the small mocked UI selection without a DB.
  `python3 scripts/check.py backend` runs the small API/worker selection and needs
  `BOOK_TEST_DATABASE_URL`. `npm --prefix apps/web run test:e2e` runs three live
  browser journeys and needs `BOOK_E2E_DATABASE_URL`.
- Full suites require explicit `--full`; reserve them for CI/release checks or
  when the scope of the change justifies them. Do not start overlapping suites,
  use `-n auto`, increase workers, or inflate timeouts to hide a failure.
- Use `--list` for discovery. Browser checks build once; `--skip-build` is only
  appropriate when the existing build includes all current frontend edits.
- Retain source/user changes. Paperclip workspace tests are retired here and must
  not be launched. See `docs/TEST-AUDIT.md` for the audit and coverage decisions.
