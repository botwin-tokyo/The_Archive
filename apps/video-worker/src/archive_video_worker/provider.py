"""yt-dlp library runs only inside the leased acquisition process group."""
import hashlib,json,math,os,re,shutil,subprocess,time
from pathlib import Path
from .policy import canonical_url,MAX_BYTES
from .server import disk_used

class Quiet:
    def debug(self,*args): pass
    def warning(self,*args): pass
    def error(self,*args): pass

def options():
    return {'quiet':True,'no_warnings':True,'logger':Quiet(),'noplaylist':True,'cachedir':False,
            'socket_timeout':20,'retries':2,'fragment_retries':2,'concurrent_fragment_downloads':1,
            'max_filesize':MAX_BYTES,'ffmpeg_location':'/usr/bin/ffmpeg','js_runtimes':{'deno':{}},
            'remote_components':set(),'enable_file_urls':False,'overwrites':False,
            'extractor_retries':2,'skip_unavailable_fragments':False}

def validate_info(info,url):
    if not isinstance(info,dict) or info.get('_type','video')!='video' or info.get('entries') is not None: raise ValueError('MULTI_VIDEO_UNSUPPORTED')
    if info.get('is_live') or info.get('live_status') in ('is_live','is_upcoming','post_live') or info.get('has_drm'): raise ValueError('LIVE_OR_DRM_UNSUPPORTED')
    duration=info.get('duration')
    if not isinstance(duration,(int,float)) or not math.isfinite(duration) or not 0<duration<=24*3600: raise ValueError('UNSUPPORTED_DURATION')
    extractor=info.get('extractor_key','').lower()
    if extractor not in ('youtube','twitter') or ('youtube.com' in url)!=(extractor=='youtube'): raise ValueError('UNSUPPORTED_SOURCE')
    identity=str(info.get('id',''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',identity): raise ValueError('UNSUPPORTED_SOURCE')
    if extractor=='youtube' and identity!=url.split('v=')[-1]: raise ValueError('SOURCE_CHANGED')
    formats=info.get('formats',[])
    if not isinstance(formats,list) or len(formats)>1000: raise ValueError('FORMAT_LIMIT')
    return {'title':str(info.get('title') or 'Untitled video')[:200],'uploader':str(info.get('uploader') or '')[:200],
            'duration':float(duration),'source':extractor+':'+identity}

def choices(info):
    formats=info.get('formats',[])
    aac=any((str(f.get('acodec','')).startswith(('mp4a','aac')) or (f.get('acodec') is None and f.get('ext') in ('mp4','m4a'))) and f.get('vcodec')=='none' and not f.get('has_drm') for f in formats)
    compatible=[f for f in formats if str(f.get('vcodec','')).startswith(('avc','h264')) and f.get('height') and not f.get('has_drm') and (str(f.get('acodec','')).startswith(('mp4a','aac')) or (f.get('acodec')=='none' and aac))]
    result=[]
    for height in sorted({int(f['height']) for f in compatible if 0<int(f['height'])<=4320},reverse=True)[:12]:
        result.append({'id':'video-'+str(height),'label':str(height)+'p · MP4','kind':'video','height':height,'estimatedBytes':None})
    if any((f.get('acodec') not in (None,'none') or (f.get('vcodec')=='none' and f.get('ext') in ('mp4','m4a'))) and not f.get('has_drm') for f in formats):result.append({'id':'audio','label':'Audio only · MP3','kind':'audio','height':0,'estimatedBytes':None})
    if not result:raise ValueError('NO_COMPATIBLE_FORMAT')
    return result

def resolve(payload):
    from yt_dlp import YoutubeDL
    url=canonical_url(payload['url'])
    with YoutubeDL(options()) as ydl:info=ydl.extract_info(url,download=False)
    return dict(validate_info(info,url),choices=choices(info))

def verify(path,kind,height,duration):
    # Bounded fields and one input, no source URLs passed to ffprobe.
    result=subprocess.run(['/usr/bin/ffprobe','-v','error','-show_entries','format=format_name,duration:stream=codec_type,codec_name,height','-of','json',str(path)],capture_output=True,timeout=30,check=True)
    if len(result.stdout)>65536:raise ValueError('INVALID_MEDIA')
    data=json.loads(result.stdout);streams=data.get('streams',[]);types=[s.get('codec_type') for s in streams]
    actual=float(data.get('format',{}).get('duration',0))
    if not math.isfinite(actual) or actual<=0 or abs(actual-duration)>max(5,duration*.02):raise ValueError('INVALID_DURATION')
    if kind=='audio':
        if data['format']['format_name']!='mp3' or types!=['audio'] or streams[0]['codec_name']!='mp3':raise ValueError('INVALID_MEDIA')
    else:
        if 'mp4' not in data['format']['format_name'].split(',') or types.count('video')!=1 or types.count('audio')!=1 or len(types)!=2:raise ValueError('INVALID_MEDIA')
        if any(s['codec_name']!=('h264' if s['codec_type']=='video' else 'aac') for s in streams):raise ValueError('INVALID_MEDIA')
        if next(s['height'] for s in streams if s['codec_type']=='video')!=height:raise ValueError('QUALITY_CHANGED')

def download(payload,directory,progress):
    from yt_dlp import YoutubeDL
    url=canonical_url(payload['url']);choice=payload['choiceId'];progress('downloading',None)
    with YoutubeDL(options()) as ydl:info=ydl.extract_info(url,download=False)
    meta=validate_info(info,url);available=choices(info)
    selected=next((c for c in available if c['id']==choice),None)
    if not selected:raise ValueError('QUALITY_UNAVAILABLE')
    if meta['source']!=payload['source'] or abs(meta['duration']-payload['duration'])>2:raise ValueError('SOURCE_CHANGED')
    last=[0.0]
    def hook(value):
        now=time.monotonic()
        if now-last[0]<.5:return
        last[0]=now
        used=disk_used(directory)
        if used>24*1024**3 or shutil.disk_usage(directory).free<10*1024**3:raise ValueError('DISK_BUDGET')
        total=value.get('total_bytes') or value.get('total_bytes_estimate');done=value.get('downloaded_bytes',0)
        progress('downloading',min(.99,max(0,done/total)) if total else None)
    opts=options();opts.update(outtmpl=str(directory/'source.%(ext)s'),progress_hooks=[hook])
    if choice=='audio':opts.update(format='bestaudio/best',postprocessors=[{'key':'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'0'}])
    else:
        h=selected['height'];opts.update(format=f'bv[vcodec^=avc][height={h}]+ba[acodec^=mp4a]/bv[vcodec^=avc][height={h}]+ba[ext=mp4]/b[vcodec^=avc][acodec^=mp4a][height={h}]',merge_output_format='mp4',postprocessors=[{'key':'FFmpegVideoRemuxer','preferedformat':'mp4'}])
    with YoutubeDL(opts) as ydl:ydl.process_ie_result(info,download=True)
    ext='mp3' if choice=='audio' else 'mp4';source=directory/('source.'+ext)
    if not source.is_file() or source.is_symlink() or not 0<source.stat().st_size<=MAX_BYTES:raise ValueError('INVALID_ARTIFACT')
    progress('validating',None);verify(source,selected['kind'],selected['height'],meta['duration'])
    final=directory/('media.'+ext);source.replace(final);os.chmod(final,0o640)
    sha=hashlib.sha256()
    with final.open('rb') as f:
        while data:=f.read(1024*1024):sha.update(data)
    # Remove intermediate source tracks; retain only verified media and bounded task files.
    for file in directory.glob('source.*'):
        if file.is_file() and not file.is_symlink():file.unlink()
    return {'filename':final.name,'sizeBytes':str(final.stat().st_size),'sha256':sha.hexdigest()}
