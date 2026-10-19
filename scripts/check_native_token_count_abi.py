"""Certify an explicitly supplied local token-count library; never compile or download."""
import argparse
import ctypes as c
import hashlib
import json
import random
import concurrent.futures
from pathlib import Path
import tiktoken
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--library', required=True, type=Path)
parser.add_argument('--build-profile', choices=['debug','release','unspecified'], default='unspecified')
args=parser.parse_args()
path=args.library.absolute()
if not path.is_file():
    raise SystemExit('library must be an existing regular file or sealed file descriptor')
lib=c.CDLL(str(path))
class Span(c.Structure):
    _fields_=[('data',c.c_void_p),('length',c.c_size_t)]
lib.tofu_token_count_abi_version.restype=c.c_uint32
lib.tofu_token_count_open.argtypes=[c.c_uint32,c.POINTER(c.c_uint64)]
lib.tofu_token_count_batch.argtypes=[c.c_uint64,c.POINTER(Span),c.c_size_t,c.POINTER(c.c_uint64),c.c_size_t]
lib.tofu_token_count_close.argtypes=[c.c_uint64]
assert lib.tofu_token_count_abi_version()==1
rng=random.Random(8123)
pieces=['hello','世界','🙂','\n','\t',' café','<|endoftext|>','a'*128,' ','\x00','e\u0301']
texts=['']+[''.join(rng.choices(pieces,k=rng.randrange(1,30))) for _ in range(1000)]
checks=0
for encoding,name in enumerate(['cl100k_base','o200k_base']):
    handle=c.c_uint64();assert lib.tofu_token_count_open(encoding,c.byref(handle))==0
    oracle=tiktoken.get_encoding(name)
    def count(batch):
        buffers=[c.create_string_buffer(text.encode()) for text in batch]
        spans=(Span*len(batch))(*[Span(c.cast(buf,c.c_void_p),len(text.encode())) for buf,text in zip(buffers,batch)])
        out=(c.c_uint64*len(batch))()
        assert lib.tofu_token_count_batch(handle,spans,len(batch),out,len(batch))==0
        assert list(out)==[len(oracle.encode(text,disallowed_special=())) for text in batch]
    count(texts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(count,[texts[i:i+100] for i in range(0,len(texts),100)]))
    bad=c.create_string_buffer(b'\xff');spans=(Span*1)(Span(c.cast(bad,c.c_void_p),1));out=(c.c_uint64*1)(12345)
    assert lib.tofu_token_count_batch(handle,spans,1,out,1)==3 and out[0]==12345
    assert lib.tofu_token_count_batch(handle,spans,1,out,0)==7 and out[0]==12345
    assert lib.tofu_token_count_batch(handle,None,0,None,0)==0
    assert lib.tofu_token_count_close(handle)==0
    assert lib.tofu_token_count_batch(handle,None,0,None,0)==4
    assert lib.tofu_token_count_close(handle)==4
    checks+=len(texts)*2
print(json.dumps({'format':'tofu.native-token-count-abi-smoke/v1','library_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'build_profile':args.build_profile,'exact_count_comparisons':checks,'concurrent_callers':4,'encodings':['cl100k_base','o200k_base'],'failure_publication':'unchanged','stale_handle':'rejected','performance_admitted':False}))
