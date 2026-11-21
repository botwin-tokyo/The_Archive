"""Run isolated provider tests with the pinned video or compatible music venv."""
import os,pathlib,subprocess,sys
r=pathlib.Path(__file__).resolve().parents[1]
env=dict(os.environ,PYTHONPATH=str(r/'apps/video-worker/src'))
subprocess.run([sys.executable,'-m','unittest','discover','-s',str(r/'apps/video-worker/tests'),'-v'],env=env,check=True)
