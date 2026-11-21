"""Mobile application boundary, sharing the existing service/domain authority."""
import hashlib
from .contracts import BackendError
from .catalog import present_entry
from .mobile_auth import MobileAuth
from .mobile_enrollment import load_replay_key

CAPABILITIES = ('list','read','write','mkdir','rename','move','copy','trash','restore','purge','capacity','folderSearch','resumableUpload','resumableDownload','atomicKeepBoth','replaceViaTrash','safePreview','saveAs')


class MobileBoundary:
    def __init__(self, service, config, *, replay_key=None):
        self.service, self.config = service, config
        self.auth = MobileAuth(service.state,service.auth,replay_key if replay_key is not None else load_replay_key(config['replayKeyFile']))
        self.limits = {'networkTransfers':2,'perDirection':1,'maxPageSize':500,
                       'maxAcceptedFileBytes':config.get('maxAcceptedFileBytes',str(64*1024**3)),
                       'stagingBudgetBytes':config.get('stagingBudgetBytes',str(128*1024**3))}
        from .mobile_acquisitions import MobileAcquisitions
        self.acquisitions = MobileAcquisitions(self)
        self.transfers = None
        from .mobile_metadata import MobileMetadata
        self.metadata = MobileMetadata(self)
        from .mobile_transfers import MobileTransfers
        self.transfers = MobileTransfers(self)

    def close(self):
        if self.transfers: self.transfers.close()

    def discovery(self):
        return {'schemaVersion':1,'protocol':'archive_mobile_file_v1','serverId':self.auth.server_id,
                'minimumClientContract':1,'supportedContractVersions':[1],'authentication':'password','limits':self.limits}

    def _caps(self, principal, drive=None, path='', available=True):
        scopes=set(principal.scopes)
        enabled={'list': 'read' in scopes, 'read':'read' in scopes, 'copy':'read' in scopes,
                 'capacity':'read' in scopes,'folderSearch':'read' in scopes,
                 **{k:'write' in scopes for k in ('write','mkdir','rename','move','atomicKeepBoth')},
                 'trash':'trash' in scopes,'restore':{'trash','write'}<=scopes,
                 'replaceViaTrash':{'trash','write'}<=scopes}
        # Native-only preview/export are not NAS capabilities. Transfers become
        # supported when their bounded service is installed in BE-05/06.
        if self.transfers and self.transfers.supports_upload:
            enabled['resumableUpload']='write' in scopes
        if self.transfers and self.transfers.supports_download:
            enabled['resumableDownload']='read' in scopes
        return [{'name':name,'supported':name in enabled,'allowed':bool(enabled.get(name,False)),
                 'availability':'available' if available else 'volume_unavailable',
                 'reason':None if name in enabled and enabled[name] and available else 'Volume unavailable' if not available else 'Not supported by this server' if name not in enabled else 'Permission not assigned'} for name in CAPABILITIES]

    def profile(self, context):
        principal=self.service.auth.refresh(context.principal); roots=[]; capacities=[]
        for drive,path in principal.roots:
            rid=hashlib.sha256((drive+'\0'+path).encode()).hexdigest()[:32]
            fallback={'driveId':drive,'path':path,'id':rid,'revision':'0','generation':'0'}
            available=False; ref=fallback
            try:
                self.service.auth.authorize(principal,'read',drive,path)
                fs=self.service.catalog.filesystem(drive); raw=fs.stat(path)
                if raw.kind!='directory': raise BackendError('UNSUPPORTED_ENTRY','Assigned root is not a folder')
                entry=present_entry(raw,drive,self.service.auth,principal)
                ref={k:entry[k] for k in ('driveId','path','id','revision')}
                ref.update(revision=str(ref['revision']),generation=str(fs.volume.generation)); available=True
                capacity=fs.volume.capacity()
                total=capacity.get('totalBytes',capacity.get('total')); free=capacity.get('availableBytes',capacity.get('available'))
                capacities.append((drive,rid,total,free))
            except BackendError as error:
                if error.code not in ('DRIVE_UNAVAILABLE','NOT_FOUND','PERMISSION_DENIED','PERMISSION_REVOKED','UNSUPPORTED_ENTRY','VOLUME_CHANGED'): raise
            roots.append({'id':rid,'label':(path.rsplit('/',1)[-1] if path else drive)[:100],
                          'ref':ref,'available':available,'capabilities':self._caps(principal,drive,path,available)})
        capacity=None
        if capacities:
            unique={d:(total,free) for d,_,total,free in capacities}
            known=all(t is not None and f is not None for t,f in unique.values())
            total=sum(int(t) for t,_ in unique.values()) if known else None
            used=sum(max(0,int(t)-int(f)) for t,f in unique.values()) if known else None
            capacity={'scope':'volume' if len(unique)==1 else 'authorized_aggregate','label':'Authorized storage',
                      'rootIds':[r for _,r,_,_ in capacities],'usedBytes':str(used) if known else None,'totalBytes':str(total) if known else None}
        return {'schemaVersion':1,'serverId':self.auth.server_id,'accountId':context.account_id,
                'deviceSessionId':context.session_id,'capabilitiesVersion':'mobile-v1.1','roots':roots,
                'capabilities':self._caps(principal,available=any(r['available'] for r in roots)),
                'limits':self.limits,'capacity':capacity,'route':'unknown'}

    def call(self, context, request):
        return self.metadata.call(context,request)
