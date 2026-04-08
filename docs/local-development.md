# Local Development

## Canonical Runtime

Boss should be started and checked with the project venv interpreter:

```bash
cd /Users/tj/boss
/Users/tj/boss/.venv/bin/python -m uvicorn boss.api:app --host 127.0.0.1 --port 8321
```

The convenience launcher uses the same interpreter:

```bash
cd /Users/tj/boss
./start-server.sh
```

## Health Checks

Run the local doctor to verify the venv, runtime packages, lock/process agreement, and live status payload:

```bash
cd /Users/tj/boss
/Users/tj/boss/.venv/bin/python scripts/dev_doctor.py
```

Run the safe smoke test against the local backend:

```bash
cd /Users/tj/boss
/Users/tj/boss/.venv/bin/python scripts/smoke_local.py
```

Enable a live chat roundtrip only when you explicitly want it:

```bash
cd /Users/tj/boss
BOSS_SMOKE_CHAT=1 /Users/tj/boss/.venv/bin/python scripts/smoke_local.py
```

## Verification

Backend regression harness:

```bash
cd /Users/tj/boss
/Users/tj/boss/.venv/bin/python -m unittest tests.test_regression_harness
```

Backend compile check:

```bash
cd /Users/tj/boss
/Users/tj/boss/.venv/bin/python -m compileall /Users/tj/boss/boss
```

macOS app build:

```bash
cd /Users/tj/boss/BossApp
swift build
```

## Notes

- The doctor and smoke scripts are local-only and do not send remote telemetry.
- Startup will report stale or mismatched lock/process state clearly, but it will not kill any process automatically.
- The live `/api/system/status` payload now includes process, interpreter, workspace, build marker, and lock-trust fields so local tooling can detect drift quickly.
- The macOS app will try to start the local backend automatically on launch when it can locate the workspace and `.venv` runtime. If a different server is already bound to `127.0.0.1:8321`, the app will warn instead of killing it.