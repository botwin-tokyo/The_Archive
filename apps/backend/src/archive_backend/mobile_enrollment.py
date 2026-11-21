"""Explicit private operator enrollment/recovery; never a mobile HTTP endpoint."""
import argparse
import getpass
import json
import os
import stat
from pathlib import Path

from .contracts import BackendError
from .persistence import State
from .security import Auth
from .mobile_auth import MobileAuth


def load_replay_key(path):
    """Read an operator-supplied private key without following a final symlink."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise BackendError('INVALID_ARGUMENT', 'Configure an absolute private mobile replay-key path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd); parent = os.stat(str(Path(path).parent), follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid() or parent.st_mode & 0o022):
            raise BackendError('PERMISSION_DENIED', 'Mobile replay key must be privately owned in a protected directory')
        key = os.read(fd, 128).strip()
        if len(key) != 44:
            raise BackendError('INVALID_ARGUMENT', 'Mobile replay key is not a Fernet key')
        from cryptography.fernet import Fernet
        try: Fernet(key)
        except (ValueError, TypeError):
            raise BackendError('INVALID_ARGUMENT', 'Mobile replay key is not a Fernet key') from None
        return key
    finally: os.close(fd)


def main():
    parser = argparse.ArgumentParser(description='Private Archive Mobile account administration; stop service first')
    parser.add_argument('--config', required=True)
    parser.add_argument('--replay-key-file', required=True)
    parser.add_argument('action', choices=('enroll', 'recover-password', 'revoke-device', 'assign','recover-restored-state'))
    parser.add_argument('--acknowledge-reconciled-state',action='store_true',help='Owner has inspected NAS effects, request history and all legacy credentials after restore')
    parser.add_argument('--username')
    parser.add_argument('--session-id')
    parser.add_argument('--roots-json', help='Explicit logical root objects; no host paths')
    parser.add_argument('--scopes', default='read,write,trash')
    args = parser.parse_args()
    from .config import parse_config
    with open(args.config, encoding='utf-8') as source: config = parse_config(json.load(source))
    if not config.state_path: raise BackendError('INVALID_ARGUMENT', 'Configure private service state first')
    recovery=args.action=='recover-restored-state'
    if recovery and (not args.acknowledge_reconciled_state or not args.roots_json):
        raise BackendError('INVALID_ARGUMENT','Restore recovery requires explicit reconciled-state acknowledgement and new root assignment')
    state = State(config.state_path,mobile_recovery=recovery)
    try:
        state.acquire_service_lock()
        auth = Auth(state)
        owner = auth.authenticate(getpass.getpass('Existing owner administration secret (hidden): '))
        auth.owner(owner)
        if recovery:
            if input('Type RESET MOBILE IDENTITY after reviewing NAS effects and legacy credentials: ')!='RESET MOBILE IDENTITY':
                raise BackendError('PERMISSION_DENIED','Restore recovery was not confirmed')
            password=getpass.getpass('New mobile password (hidden): ')
            if password!=getpass.getpass('Confirm new mobile password (hidden): '): raise BackendError('INVALID_ARGUMENT','Password confirmation did not match')
            from .mobile_guard import recover_mobile_identity
            print(json.dumps(recover_mobile_identity(state,auth,owner,password,args.scopes.split(','),json.loads(args.roots_json),acknowledged=True)))
            return
        mobile = MobileAuth(state, auth, load_replay_key(args.replay_key_file))
        if args.action in ('enroll', 'recover-password'):
            password = getpass.getpass('New mobile password (hidden): ')
            if password != getpass.getpass('Confirm mobile password (hidden): '):
                raise BackendError('INVALID_ARGUMENT', 'Password confirmation did not match')
        if args.action == 'enroll':
            result = mobile.enroll(owner, args.username, password, args.scopes.split(','), json.loads(args.roots_json or '[]'))
        elif args.action == 'recover-password': result = mobile.recover_password(owner, password)
        elif args.action == 'assign': result = mobile.assign(owner, args.scopes.split(','), json.loads(args.roots_json or '[]'))
        else: result = mobile.revoke_device(owner, args.session_id)
        print(json.dumps(result))
    finally: state.close()


if __name__ == '__main__': main()
