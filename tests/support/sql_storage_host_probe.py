"""Resident-host PostgreSQL qualification using the existing explicit test port.

The distributed deployment preview fence stays unchanged. Only this disposable
test entry point selects the PostgreSQL fixture through StorageSupervisor's
existing backend parameter; host lifecycle and transport are production code.
"""
from lib.storage.supervisor import StorageSupervisor
from serverctl_pkg.native_storage_host import main


if __name__ == '__main__':
    raise SystemExit(main(supervisor_factory=lambda: StorageSupervisor(
        backend='postgres', connection_file='')))
