"""Account-scoped acquisition authority; never elevates a mobile session to owner."""
from .contracts import BackendError
from .security import Principal


def principal(auth, value):
    return auth.authorize(Principal(value['id'], value['label'], value['kind'],
        value['credentialId'], value['scopeVersion'], tuple(value['scopes']),
        tuple((r['driveId'], r['path']) for r in value['roots'])), 'acquire')


def visible(auth, actor, row):
    actor = auth.authorize(actor, 'acquire')
    if actor.kind != 'owner' and row.get('_actor', {}).get('id') != actor.id:
        raise BackendError('NOT_FOUND', 'Acquisition record not found')
    if row.get('destination'):
        auth.authorize(actor, 'read', row['destination']['driveId'], row['destination']['path'])
    return row


def accessible(auth, actor, row):
    try:
        visible(auth, actor, row)
        return True
    except BackendError as error:
        if error.code in ('NOT_FOUND', 'PERMISSION_DENIED'): return False
        raise
