# Memory Client Verification

## Automated Checks

Run the lightweight regression harness:

```bash
cd /Users/tj/boss
/Users/tj/boss/.venv/bin/python -m unittest tests.test_regression_harness
```

Validate the backend module graph still compiles:

```bash
cd /Users/tj/boss
/Users/tj/boss/.venv/bin/python -m compileall /Users/tj/boss/boss
```

Validate the macOS client still builds:

```bash
cd /Users/tj/boss/BossApp
swift build
```

## Manual Checklist

1. Start the backend with `./start-server.sh` from `/Users/tj/boss`.
2. Run `/Users/tj/boss/.venv/bin/python scripts/dev_doctor.py` and confirm it reports a healthy local runtime.
3. Run `/Users/tj/boss/.venv/bin/python scripts/smoke_local.py` and confirm status and memory endpoints pass.
4. Launch the app from `/Users/tj/boss/BossApp` and confirm chat still opens by default.
5. Open the new Memory surface from the sidebar and confirm the scan-status card renders counts and the last scan timestamp.
6. Click `Rescan` and confirm the scan-status counts refresh without forcing the UI back to Chat.
7. Select a project from the sidebar and confirm the Memory surface opens with that project summary expanded first.
8. Expand at least one project summary and confirm stack, entry points, useful commands, and notable modules are visible.
9. Send a chat turn that should match a known preference or durable memory and confirm the `Current Turn` section explains why the memory was injected.
10. Use a `Forget` action on a deletable memory item and confirm it disappears after refresh.
11. Open Permissions and confirm the existing permissions ledger still loads.
12. Start a tool action that requires approval, quit before approving if needed, reopen the app, and confirm the pending approval still resumes instead of vanishing.

## Coverage Notes

The regression harness covers:

- entry-agent naming and handoff topology
- durable memory persistence, lookup, deletion, and injection relevance
- scanner discovery, project summary generation, and entry-point extraction
- pending-run persistence and expiry archive behavior
- session context compaction and conversation-episode syncing