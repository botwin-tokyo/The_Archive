"""Additive mobile acquisition API over the existing durable music/video services."""
import re
from .contracts import BackendError, CAPABILITIES, digest, identifier, object_fields
from .music_contracts import tool_schema as music_schema, public
from .video_contracts import tool_schema as video_schema
from .acquisition_auth import visible

METHODS = tuple(sorted({
    'video.status','video.resolve','video.plan.get','video.enqueue',
    'video.jobs.list','video.jobs.get','video.jobs.control',
    'music.status','music.search','music.search.get','music.resolve','music.plan.get',
    'music.artwork.get','music.enqueue','music.jobs.list','music.jobs.get','music.jobs.control'}))
SUBMISSIONS = {'video.resolve','video.enqueue','music.search','music.resolve','music.enqueue'}


def input_schema(method):
    return (video_schema if method.startswith('video.') else music_schema)(CAPABILITIES[method])


def validate(value, schema):
    """Validate the bounded subset used by existing acquisition input schemas."""
    kind=schema.get('type')
    if kind=='object':
        object_fields(value,schema.get('required',()),set(schema['properties'])-set(schema.get('required',())))
        for key,item in value.items(): validate(item,schema['properties'][key])
    elif kind=='string':
        if not isinstance(value,str) or not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',4096):
            raise BackendError('INVALID_ARGUMENT','Invalid text field')
        if '\0' in value or any(0xD800<=ord(c)<=0xDFFF for c in value):
            raise BackendError('INVALID_ARGUMENT','Invalid text encoding')
        if schema.get('pattern') and re.fullmatch(schema['pattern'],value) is None:
            raise BackendError('INVALID_ARGUMENT','Invalid field format')
    elif kind=='integer':
        if type(value) is not int or not schema.get('minimum',0)<=value<=schema.get('maximum',2**53-1):
            raise BackendError('INVALID_ARGUMENT','Invalid integer field')
    if 'enum' in schema and value not in schema['enum']:
        raise BackendError('INVALID_ARGUMENT','Unsupported option')


class MobileAcquisitions:
    def __init__(self,boundary):
        self.boundary=boundary;self.service=boundary.service

    def discovery(self,context):
        actor=self.service.auth.refresh(context.principal)
        allowed='acquire' in actor.scopes
        integrations=[]
        for name,service in [('video',self.service.video),('music',self.service.music)]:
            configured=bool(service.config)
            available=bool(service.available if name=='video' else service.health['available'])
            integrations.append({'id':name,'label':'Yoinks' if name=='video' else 'SpotifyDL',
                'configured':configured,'available':configured and available,'allowed':allowed,
                'reason':None if allowed and configured and available else 'Permission not assigned' if not allowed else 'Worker not configured' if not configured else 'Worker unavailable'})
        return {'schemaVersion':1,'protocol':'archive_mobile_acquisition_v1','methods':list(METHODS)+['acquisitions.reconcile'],
            'integrations':integrations,'maxRoots':128,'pollSeconds':2,'push':False,
            'jobOwnership':'account','continuesAfterLogout':True,'searchKinds':['track','album','artist'],
            'playlistSupported':False,'supportedSites':['YouTube','X / Twitter']}

    def storage(self,context):
        actor=self.service.auth.refresh(context.principal)
        return {'schemaVersion':1,'maxRoots':128,'drives':self.service.drives(actor),
                'roots':self.boundary.profile(context)['roots']}

    def call(self,context,request):
        object_fields(request,('method','params'))
        method=request['method'];args=request['params']
        if not isinstance(method,str) or method not in (*METHODS,'acquisitions.reconcile'):
            raise BackendError('UNSUPPORTED_ENTRY','Mobile acquisition method unavailable')
        actor=self.boundary.auth.acquisition_principal(context)
        if method=='acquisitions.reconcile':
            object_fields(args,('method','requestId'))
            if not isinstance(args['method'],str) or args['method'] not in SUBMISSIONS:
                raise BackendError('INVALID_ARGUMENT','Only submissions have request receipts')
            identifier(args['requestId'])
            prefix,action=args['method'].split('.',1)
            key=digest([actor.id,args['method'] if prefix=='video' else action,args['requestId']])
            receipt=self.service.state.get(prefix+'_requests',key)
            if not receipt:raise BackendError('REQUEST_UNKNOWN','No acquisition receipt exists for this account/request')
            service=getattr(self.service,prefix)
            row=visible(self.service.auth,actor,service.get(receipt['namespace'],receipt['id']))
            return {'method':args['method'],'requestId':args['requestId'],'result':public(row)}
        validate(args,input_schema(method))
        service=self.service.video if method.startswith('video.') else self.service.music
        return service.call(actor,method,args)
