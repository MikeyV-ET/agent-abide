#!/usr/bin/env python3
"""
Localmail — backward compatibility shim.

Re-exports from localmail_adapter (agent-side API) and
localmail_service (daemon). Existing imports continue to work.

New code should import from the specific module:
  from localmail_adapter import send_mail, reply_all, read_mail
  # or run localmail_service.py as the daemon
"""

# Agent-side API (send, read)
from localmail_adapter import send_mail, reply_all, read_mail, peek_mail

# Service-side (daemon entry point + doorbell)
from localmail_service import (
    ring_doorbell, get_asdaaas_agents, watch_loop,
    deliver_to_inbox, process_outboxes, process_inboxes,
    cleanup_old_payloads,
)

# Main entry point — runs the service daemon
import sys
import os

def main():
    """Run the localmail service daemon (backward compat entry point)."""
    from localmail_service import main as service_main
    service_main()


if __name__ == "__main__":
    main()
