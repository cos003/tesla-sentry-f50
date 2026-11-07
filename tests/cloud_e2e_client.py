"""Cross-project test client. Generates a synthetic SEI MP4 container, not a car recording."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import urllib.error

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('sync',ROOT/'sentry-cloud-sync.py')
sync=importlib.util.module_from_spec(spec); spec.loader.exec_module(sync)
work=Path(sys.argv[1]); server=sys.argv[2]
if server == '--stdio':
    # Sandbox-compatible transport: exercise real HTTP route handlers over process pipes.
    # This does not test TCP/TLS; production still uses urllib with HTTPS verification.
    server = 'https://cloud.test'
    def rpc_post(server, endpoint, token, body):
        print(json.dumps({'type':'request','path':endpoint,'headers':{
            'Authorization':'Bearer '+token,'Content-Type':'application/json'},'body':body}),flush=True)
        response=json.loads(sys.stdin.readline())
        if response['status'] >= 400:
            raise urllib.error.HTTPError(server+endpoint,response['status'],'test transport',{},None)
        payload=response['body']
        assert payload.get('ok') is True
        return payload['data']
    sync.post = rpc_post
archive=work/'archive'
folder=archive/'RecentClips'/'2026-01-01_08-00-00'; folder.mkdir(parents=True)
video=folder/'2026-01-01_08-00-00-front.mp4'
subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc=size=160x120:rate=10',
                '-t','2','-c:v','libx264','-preset','ultrafast','-g','1','-pix_fmt','yuv420p',str(video)],check=True,capture_output=True)
os.environ['TSF_CACHE']=str(work/'cache')
spec=importlib.util.spec_from_file_location('viewer',ROOT/'sentry-viewer.py')
viewer=importlib.util.module_from_spec(spec); spec.loader.exec_module(viewer)
offs,length=viewer._mp4_video_samples(video)
assert length==4 and len(offs)>=20
# Keep the MP4 tables/offsets and duration intact; replace sample contents with test SEI NALs.
with video.open('r+b') as f:
    for i,(offset,size) in enumerate(offs):
        state=1 if i<10 else 2
        nal=b'\x06\x05\xffBBi\x08\x01\x50'+bytes([state])
        assert size>len(nal)+5
        padding=size-5-len(nal)
        # Valid protobuf padding of either parity; incomplete varints are rejected by the parser.
        if padding % 2:
            nal+=b'\x78\x81\x01'; padding-=3
        nal+=b'\x78\x00'*(padding//2)+b'\x80'
        f.seek(offset); f.write(struct.pack('>I',len(nal))+nal)
os.utime(video,(time.time()-60,)*2)
config=sync.post(server,'/api/sentry/enroll',os.environ['TEST_PAIRING_CODE'],{'name':'E2E Pi'})
config['server']=server
config_file=work/'cloud.json'; sync.private_json(config_file,config)
result=sync.main(['run','--once','--allow-local-http','--config',str(config_file),
                  '--state',str(work/'queue.sqlite'),'--archive',str(archive),'--viewer',str(ROOT/'sentry-viewer.py')])
assert result==0
db=sync.open_queue(work/'queue.sqlite')
assert db.execute('SELECT count(*) FROM clips WHERE pending=1').fetchone()[0]==0
db.close()
video.unlink()  # Neither the device nor its video is needed for subsequent cloud queries.
print('Synthetic MP4 → real extractor → persistent queue → cloud route contract: OK')
