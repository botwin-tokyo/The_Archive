"""Run isolated anime worker tests. Standard library only; FFmpeg/ffprobe must be installed."""
import os,pathlib,subprocess,sys
r=pathlib.Path(__file__).resolve().parents[1]
env=dict(os.environ,PYTHONPATH=str(r/'apps/anime-worker/src'))
subprocess.run([sys.executable,'-m','unittest','discover','-s',str(r/'apps/anime-worker/tests'),'-v'],env=env,check=True)
