"""Bounded MP3 inspection after the source downloader returns."""
import hashlib
import json
import os
import stat
import subprocess


def verify(path, expected_ms=0):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 512*1024*1024:
        raise ValueError('Invalid media artifact')
    raw = subprocess.run(['/usr/bin/ffprobe', '-v', 'error', '-show_entries',
                          'stream=codec_name,codec_type:format=duration', '-of', 'json', str(path)],
                         capture_output=True, timeout=30, check=True)
    if len(raw.stdout) > 65536: raise ValueError('Probe too large')
    data = json.loads(raw.stdout); duration = float(data['format']['duration'])
    if not any(s.get('codec_name') == 'mp3' and s.get('codec_type') == 'audio' for s in data['streams']):
        raise ValueError('MP3 audio required')
    if duration <= 0 or expected_ms and abs(duration*1000-expected_ms) > max(15000, expected_ms*.15):
        raise ValueError('Source duration mismatch')
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024*1024): checksum.update(chunk)
        os.fsync(stream.fileno())
    os.chmod(path, 0o640)
    return {'sha256': checksum.hexdigest(), 'sizeBytes': str(info.st_size), 'durationMs': int(duration*1000)}


def artwork(images):
    """Optional preview: fixed Spotify image host, no redirects, bounded decode."""
    import base64
    import io
    import urllib.request
    from urllib.parse import urlsplit
    from PIL import Image
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl): return None
    try:
        candidates=[i for i in images if 0<int(i.get('width') or 0)<=640]
        if not candidates: return None
        url=sorted(candidates,key=lambda i:i['width'])[-1]['url'];parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!='i.scdn.co' or parsed.username or parsed.password or parsed.port not in (None,443): return None
        with urllib.request.build_opener(NoRedirect).open(url,timeout=8) as response: data=response.read(2*1024*1024+1)
        if len(data)>2*1024*1024: return None
        with Image.open(io.BytesIO(data)) as image:
            if image.width>2048 or image.height>2048 or image.format not in ('JPEG','PNG','WEBP'): return None
            image.thumbnail((320,320));output=io.BytesIO();image.convert('RGB').save(output,format='JPEG',quality=75)
        encoded='data:image/jpeg;base64,'+base64.b64encode(output.getvalue()).decode()
        return encoded if len(encoded)<=128*1024 else None
    except Exception: return None
