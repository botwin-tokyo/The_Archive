"""Explicit vault HTTP route mapping; authentication stays in Service.call."""
import re
from .contracts import BackendError

def route(method,path,body):
 routes={('GET','/vault/v1/status'):'status',('POST','/vault/v1/entries/search'):'entries.list',('POST','/vault/v1/entries'):'entries.create',('POST','/vault/v1/passwords/generate'):'passwords.generate',('POST','/vault/v1/requests/reconcile'):'requests.reconcile',('POST','/vault/v1/backup/export'):'backup.export',('POST','/vault/v1/backup/import'):'backup.import'}
 op=routes.get((method,path));args=dict(body)
 if not op:
  match=re.fullmatch(r'/vault/v1/entries/([a-zA-Z0-9-]{1,256})(?:/(reveal|trash|restore|history|restore-version))?',path)
  if match:
   ident,action=match.groups();op={('GET',None):'entries.get',('PATCH',None):'entries.update',('POST','reveal'):'entries.reveal',('POST','trash'):'entries.trash',('POST','restore'):'entries.restore',('POST','history'):'entries.history',('POST','restore-version'):'entries.restoreVersion'}.get((method,action))
   if 'id' in args:raise BackendError('INVALID_ARGUMENT','Entry identity belongs in the route')
   args['id']=ident
 if not op:raise BackendError('NOT_FOUND','Unknown vault route')
 return {'version':1,'capability':'vault.'+op,'arguments':args}
