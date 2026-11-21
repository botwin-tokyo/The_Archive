"""Explicit local owner enrollment. Never called by startup or tests implicitly."""
import getpass
import json
from .contracts import BackendError
from .persistence import State
from .security import Auth


def enroll_owner(state_path):
    if not state_path: raise BackendError('INVALID_ARGUMENT','Configure a private service-state path first')
    token=getpass.getpass('Paste the owner secret from your password manager (not displayed): ')
    confirmation=getpass.getpass('Confirm the same owner secret: ')
    if token!=confirmation: raise BackendError('INVALID_ARGUMENT','Owner secret confirmation did not match')
    state=State(state_path)
    try:
        state.acquire_service_lock()
        owner=Auth(state).bootstrap_owner(token)
        print(json.dumps({'enrolled':True,'principalId':owner.id,'credentialId':owner.credential_id}))
    finally: state.close()
