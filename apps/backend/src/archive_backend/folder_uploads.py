"""Durable parent receipts for bounded, sequential native folder uploads."""
from .contracts import BackendError, bytes_value, identifier, location, name
from .operations import join


def begin(transfers, principal, args):
    transfers.auth.owner(principal)
    location(args['destination']);name(args['name']);identifier(args['requestId'])
    total=bytes_value(args['sizeBytes']);count=args['fileCount'];fingerprint=args['manifestDigest']
    import re
    if type(count) is not int or not 0<=count<=250000 or not isinstance(fingerprint,str) or not re.fullmatch('[a-f0-9]{64}',fingerprint):
        raise BackendError('INVALID_ARGUMENT','Invalid bounded folder manifest')
    target=join(args['destination']['path'],args['name']);drive=args['destination']['driveId']
    transfers.auth.authorize(principal,'write',drive,target)
    fs=transfers.catalog.filesystem(drive)
    payload={'kind':'upload','folderUpload':True,'destination':args['destination'],'name':args['name'],'bytesTotal':str(total),'fileCount':count,'manifestDigest':fingerprint}
    with transfers.state.transaction():
        previous=transfers.state.reconcile(principal.id,'upload',args['requestId'])
        if not previous:
            try:fs.stat(target)
            except BackendError as e:
                if e.code!='NOT_FOUND':raise
            else:raise BackendError('CONFLICT','This folder already exists at the destination. Choose a different destination; folder imports do not overwrite existing folders.')
            if fs.volume.capacity()['available']<total+transfers.min_free_bytes:raise BackendError('INSUFFICIENT_SPACE','Not enough destination space for the folder')
        receipt=transfers.jobs.accept(principal,'upload',payload,args['requestId'],initial_state='waiting-for-client')
        key=receipt['jobIds'][0]
        if not transfers.state.get('folder_uploads',key):
            transfers.state.put('folder_uploads',key,dict(id=key,requestId=args['requestId'],drive=drive,path=target,destination=args['destination'],name=args['name'],fileCount=count,sizeBytes=str(total),manifestDigest=fingerprint,completedFiles=0,completedBytes='0'))
        return dict(receipt,folderId=key)


def status(transfers,principal,key):
    transfers.auth.owner(principal);identifier(key)
    job=transfers.jobs.get(principal,key);row=transfers.state.get('folder_uploads',key)
    if not row:raise BackendError('NOT_FOUND','Folder upload is unavailable')
    transfers.auth.authorize(principal,'write',row['drive'],row['path'])
    return dict(row,state=job['state'],error=job.get('error'))


def writable(transfers,principal,key):
    row=status(transfers,principal,key)
    if row['state'] in ('paused','pausing'):raise BackendError('PAUSED','Folder upload is paused')
    if row['state'] in ('canceled','canceling'):raise BackendError('CANCELED','Folder upload was canceled')
    if row['state'] not in ('queued','waiting-for-client'):raise BackendError('CONFLICT','Folder upload is not writable')
    return row


def acknowledge(transfers,principal,key,transfer_id):
    with transfers.state.transaction():
        row=writable(transfers,principal,key)
        child=transfers._session(principal,transfer_id)
        if child.get('folderId')!=key or child['state']!='completed' or child.get('entry') is None:
            raise BackendError('CONFLICT','Only a verified file from this folder can advance its progress')
        receipt_key=key+':'+transfer_id
        if not transfers.state.get('folder_files',receipt_key):
            row['completedFiles']+=1;row['completedBytes']=str(int(row['completedBytes'])+int(child['size']))
            if row['completedFiles']>row['fileCount'] or int(row['completedBytes'])>int(row['sizeBytes']):raise BackendError('INTEGRITY_FAILED','Folder totals exceed the selected manifest')
            transfers.state.put('folder_files',receipt_key,{'id':transfer_id})
            row.pop('state',None);row.pop('error',None);transfers.state.put('folder_uploads',key,row)
            job=transfers.state.get('jobs',key);job.update(bytesDone=row['completedBytes'],state='waiting-for-client',error=None)
            transfers.jobs._save(job,'folder-progress')
    return status(transfers,principal,key)


def finish(transfers,principal,key):
    with transfers.state.transaction():
        row=status(transfers,principal,key)
        if row['state']=='completed':return row
        writable(transfers,principal,key)
        if row['completedFiles']!=row['fileCount'] or row['completedBytes']!=row['sizeBytes']:raise BackendError('TRANSFER_INTERRUPTED','Folder still has unverified files')
        if transfers.catalog.filesystem(row['drive']).stat(row['path']).kind!='directory':raise BackendError('INTEGRITY_FAILED','Folder destination is unavailable')
        job=transfers.state.get('jobs',key);job.update(state='completed',bytesDone=row['sizeBytes'],outcome='complete',resultingNames=[row['name']],error=None)
        transfers.jobs._save(job,'folder-completed')
    return status(transfers,principal,key)


def error(transfers,principal,key,code):
    with transfers.state.transaction():
        row=status(transfers,principal,key)
        if row['state'] in ('paused','pausing','canceled','canceling','completed'):return row
        messages={'SOURCE_CHANGED':'A selected file changed; restore it before resuming','INTEGRITY_FAILED':'File verification failed','FULL_DISK':'Destination space is unavailable','CONFLICT':'A destination needs attention','INTERRUPTED':'Folder upload interrupted; reselect the original folder to resume'}
        if code not in messages:code='INTERRUPTED'
        job=transfers.state.get('jobs',key);job.update(state='waiting-for-client',error=BackendError(code,messages[code],True).envelope(key))
        transfers.jobs._save(job,'folder-interrupted')
    return status(transfers,principal,key)
