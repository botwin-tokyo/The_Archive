"""Durable artist orchestration; album jobs retain existing bounded imports."""
import copy
import time
from .contracts import BackendError

FINISHED = frozenset(('completed', 'partial', 'failed', 'canceled'))


def create_releases(plan):
    return [dict(release, state='queued', jobId=None, completedTracks=0, totalTracks=release['trackCount'], error=None,
                 _planId=None, _attempt=0) for release in plan['releases']]


def control(music, principal, job, action):
    for release in job['releases']:
        child = music.get('jobs', release['jobId']) if release['jobId'] else None
        if child and child['state'] not in ('completed', 'canceled'):
            if action == 'resume' and child['state'] != 'paused': continue
            if action == 'pause' and child['state'] in FINISHED: continue
            if action == 'retry-failed' and child['state'] not in ('failed', 'partial'): continue
            music.call(principal, 'music.jobs.control', {'id':child['id'], 'revision':child['revision'], 'action':action})
            child = music.get('jobs', child['id'])
        if child:
            update_release(release, child)
        elif action == 'cancel': release['state'] = 'canceled'
        elif action == 'pause' and release['state'] not in FINISHED: release['state'] = 'paused'
        elif action == 'resume' and release['state'] == 'paused': release['state'] = 'queued'
        elif action == 'retry-failed' and release['state'] == 'failed':
            release.update(state='queued', error=None, _planId=None, _attempt=release['_attempt']+1)
    job['state'] = 'canceled' if action == 'cancel' else 'paused' if action == 'pause' else 'queued'
    music.save(job)


def update_release(release, child):
    release.update(state=child['state'], totalTracks=len(child['tracks']),
                   completedTracks=sum(t['state'] in ('completed','already-present') for t in child['tracks']),
                   error='Some tracks could not be downloaded. Open the release for details.' if child['state'] in ('partial','failed') else None)


def tick(music):
    for job in music.artist_jobs():
        if job['state'] in ('completed','canceled','paused'): continue
        if job['state'] in ('partial','failed'):
            # A release can be retried independently of its artist queue.
            # Refresh changed children without automatically retrying failures.
            for release in job['releases']:
                if release['jobId']:
                    current = dict(release)
                    update_release(current, music.get('jobs', release['jobId']))
                    if current != release: break
            else: continue
        try: principal = music.principal(job['_actor'])
        except BackendError:
            job['state']='failed'
            for release in job['releases']:
                if release['state'] not in FINISHED: release.update(state='failed',error='The requesting credential was revoked')
            music.save(job); continue
        before = repr(job)
        active = 0
        for release in job['releases']:
            if release['jobId']:
                child=music.get('jobs', release['jobId']); update_release(release, child)
                active += child['state'] not in FINISHED
        # Resolve incrementally and keep at most three albums ahead. Artist size
        # does not increase the two audio slots or require giant track documents.
        for release in job['releases']:
            if music.stop.is_set() or active >= 3: break
            if release['jobId'] or release['state'] in FINISHED: continue
            try:
                suffix=job['id']+'-'+release['id']+'-'+str(release['_attempt'])
                if not release['_planId']:
                    plan=music.call(principal,'music.resolve',{'requestId':'artist-plan-'+suffix,'selection':{'kind':'album','id':release['id']}})
                    release['_planId']=plan['id']; release['state']='resolving'
                plan=music.get('plans',release['_planId'])
                if plan['state']=='failed': release.update(state='failed',error=plan['error']); continue
                if plan['state']!='completed': break
                if plan['expiresAt']<time.time():
                    release.update(_planId=None,_attempt=release['_attempt']+1,state='queued'); break
                snapshot=copy.deepcopy(release); revision=job['revision']
                try:
                    with music.state.transaction():
                        child=music.call(principal,'music.enqueue',{'requestId':'artist-enqueue-'+suffix,'planId':plan['id'],'planRevision':plan['revision'],'destination':job['destination']},_parent=job['id'])
                        row=music.get('jobs',child['id'])
                        release['jobId']=row['id']; update_release(release,row)
                        music.save(job)
                except Exception:
                    release.clear(); release.update(snapshot); job['revision']=revision
                    raise
                active+=1
            except BackendError as error:
                if error.code in ('BUSY','QUOTA_EXCEEDED','UNAVAILABLE','DRIVE_UNAVAILABLE','INSUFFICIENT_SPACE','READ_ONLY'):
                    release['error']='Waiting for queue capacity, metadata service, or the destination drive'; break
                release.update(state='failed',error=error.message)
        states=[r['state'] for r in job['releases']]
        if all(s=='completed' for s in states): job['state']='completed'
        elif all(s in FINISHED for s in states): job['state']='partial' if any(r['completedTracks'] for r in job['releases']) else 'failed'
        elif 'waiting-for-drive' in states: job['state']='waiting-for-drive'
        else: job['state']='running'
        if repr(job)!=before: music.save(job)
