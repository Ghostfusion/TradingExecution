@echo off
rem Production launcher for the signald execution daemon.
rem
rem Started by Task Scheduler as task "Signald_Daemon" (at logon + daily 08:00 CT, restart on
rem failure), so it owns its own process and survives the terminal that started it - the
rem 2026-09-16 outage was a daemon launched from a shell that died when the window closed.
rem See docs/RUNBOOK.md, "Supervision".
rem
rem Do not use this to run the daemon by hand as the production instance: the PID lock
rem (signals/signald.pid) allows exactly one daemon, but a shell instance dies with its
rem terminal. For a one-shot check use: py -3.12 -m signald run --once
cd /d "%~dp0"
rem -u: unbuffered, so the append-only log is readable while the daemon runs.
rem Watch dir is the sibling research repo's report tree; data dir is this repo's ./signals.
py -3.12 -u -B -m signald run --watch "%~dp0..\TradingAgents\reports" --data .\signals --execute >> ".\signals\signald_daemon.log" 2>&1
