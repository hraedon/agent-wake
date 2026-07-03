# agent-waked Windows Service wrapper
#
# Per Plan 004 WI-4.1 and blueprint §3 (Windows Service for native Windows deployment).
# Requires pywin32:  pip install pywin32
#
# Install:
#   python daemon/windows_service.py install
#   python daemon/windows_service.py start
#
# Uninstall:
#   python daemon/windows_service.py stop
#   python daemon/windows_service.py remove
#
# The service reads config from %USERPROFILE%\.config\agent-wake\config.json
# (or AGENT_WAKE_CONFIG env var). Suite.env is read from
# %ProgramData%\agent-suite\suite.env (system) or
# %USERPROFILE%\.config\agent-suite\suite.env (per-user).

import sys
import os
import logging

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
except ImportError:
    print("pywin32 is required: pip install pywin32", file=sys.stderr)
    sys.exit(1)

from agent_waked.main import _run


class AgentWakedService(win32serviceutil.ServiceFramework):
    _svc_name_ = "AgentWaked"
    _svc_display_name_ = "agent-wake Daemon"
    _svc_description_ = (
        "External-to-session signaling daemon for agent harnesses. "
        "Receives wake events via HTTP and routes them to connected adapters."
    )

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._stop_event = None
        logging.basicConfig(
            filename=os.path.join(
                os.environ.get("ProgramData", "C:\\ProgramData"),
                "agent-wake", "agent-waked-service.log",
            ),
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        logging.info("service stop requested")
        if self._stop_event is not None:
            self._stop_event.set()
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        logging.info("agent-waked service starting")
        try:
            import asyncio
            asyncio.run(self._run_service())
        except Exception as e:
            logging.exception("service error: %s", e)

    async def _run_service(self):
        from agent_waked.main import _run
        await _run()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(AgentWakedService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(AgentWakedService)
