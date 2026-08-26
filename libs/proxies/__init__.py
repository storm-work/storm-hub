"""Central proxy-lease service (spec §2/§12b).

The single authority that hands out proxies from the DB pools — removing the cross-process
proxy-cursor collision that plagued storm-auto (no more --proxy-offset). Role split (operator spec):

  SOAX  -> registration + login/warmup   (sticky session, 1 use each)
  ISP   -> hold / discover / apply        (static, up to max_sessions=2 per IP)

A worker leases a SOAX proxy to warm, then leases an ISP proxy after swap.
"""
from .credentials import build_soax_credential, build_webshare_credential, soax_session_id
from .health import apply_result, check_proxy
from .lease import (
    ProxyLeaseError, conn_url, find_orphaned_worker_ids, find_reactivation_candidates, lease_proxy,
    release_proxy, release_worker_resources,
)

__all__ = [
    "ProxyLeaseError", "conn_url", "lease_proxy", "release_proxy", "release_worker_resources",
    "check_proxy", "apply_result",
    "build_soax_credential", "build_webshare_credential", "soax_session_id",
    "find_reactivation_candidates", "find_orphaned_worker_ids",
]
