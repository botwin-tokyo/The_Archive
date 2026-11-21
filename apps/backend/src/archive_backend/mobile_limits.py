"""Admission bounds never erase durable request identities to regain capacity."""
from .contracts import BackendError

REQUEST_LIMIT=100000


def reserve_request(state):
    count=state.connection.execute("SELECT COUNT(*) FROM records WHERE namespace IN ('mobile_operations','mobile_upload_requests','mobile_download_requests','mobile_manifests')").fetchone()[0]
    if count>=REQUEST_LIMIT:
        raise BackendError('QUOTA_EXCEEDED','Durable request history is full; private operator capacity review is required')
