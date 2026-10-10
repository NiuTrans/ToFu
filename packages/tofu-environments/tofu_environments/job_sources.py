"""Owned ZIP input boundary for batch providers; caller owns staging lifecycle.

Entry point extract_job_source verifies an archive's exact bytes before extracting
regular files into a caller-provided empty directory. No provider, credentials,
network, background worker or persistent cache is selected here.
"""
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, unquote
import hashlib
import os
import stat
import zipfile
import tempfile
from .contracts import EnvironmentError

def extract_job_source(actor, source, destination, *, byte_limit, file_limit, reserved=()):
    if type(byte_limit) is not int or byte_limit < 1 or type(file_limit) is not int or file_limit < 1:
        raise EnvironmentError('source_limits_required','Positive source byte/file bounds required')
    if not actor.project_root:
        raise EnvironmentError('project_root_required','Source reads require an explicit project root')
    root=Path(actor.project_root).resolve(strict=True)
    uri=urlsplit(source['uri'])
    if uri.scheme not in ('','file') or uri.netloc:
        raise EnvironmentError('source_uri_unsupported','This source port accepts owned local ZIP archives')
    path=Path(unquote(uri.path))
    if not path.is_absolute():path=root/path
    path=path.resolve(strict=True)
    try:path.relative_to(root)
    except ValueError as exc:raise EnvironmentError('source_outside_project','Source must belong to the actor project') from exc
    target=Path(destination)
    if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
        raise EnvironmentError('staging_invalid','Caller must supply an empty private staging directory')
    forbidden=set(reserved);written=[]
    try:
        with path.open('rb') as original, tempfile.TemporaryFile(dir=target) as stream:
            if os.fstat(original.fileno()).st_size>byte_limit:
                raise EnvironmentError('source_limit','Archive exceeds source byte bound')
            digest=hashlib.sha256()
            copied=0
            for chunk in iter(lambda:original.read(1024*1024),b''):
                copied+=len(chunk)
                if copied>byte_limit:raise EnvironmentError('source_limit','Source grew beyond archive bound')
                digest.update(chunk);stream.write(chunk)
            stream.flush()
            if digest.hexdigest()!=source['sha256']:
                raise EnvironmentError('source_changed','Source archive differs from its pinned hash')
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                entries=archive.infolist();names=set();files=[];total=0
                if len(entries)>file_limit:raise EnvironmentError('source_limit','Archive entry count exceeds bound')
                for entry in entries:
                    name=entry.filename;parts=PurePosixPath(name).parts
                    if not parts or name.startswith('/') or '\\' in name or '\x00' in name or '..' in parts or any(':' in x for x in parts):
                        raise EnvironmentError('source_path_invalid','Archive contains an unsafe path')
                    clean='/'.join(parts)
                    if clean in names or clean in forbidden or parts[0] in forbidden:
                        raise EnvironmentError('source_path_invalid','Duplicate or reserved archive path')
                    names.add(clean)
                    mode=entry.external_attr>>16;kind=stat.S_IFMT(mode)
                    if entry.is_dir():
                        if kind not in (0,stat.S_IFDIR):raise EnvironmentError('source_type_invalid','Invalid archive directory')
                        continue
                    if kind not in (0,stat.S_IFREG) or entry.flag_bits&1:
                        raise EnvironmentError('source_type_invalid','Only unencrypted regular source files allowed')
                    total+=entry.file_size;files.append((entry,clean))
                    if total>byte_limit or len(files)>file_limit:
                        raise EnvironmentError('source_limit','Expanded source exceeds byte/file bound')
                if not files:raise EnvironmentError('source_empty','Source archive contains no files')
                receipts=[];actual_total=0
                for entry,name in files:
                    output=target.joinpath(*PurePosixPath(name).parts);output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                    if any(p.is_symlink() for p in [output,*output.parents] if p!=target.parent):
                        raise EnvironmentError('source_path_invalid','Staging contains a symbolic link')
                    h=hashlib.sha256();count=0
                    with archive.open(entry) as incoming,output.open('xb') as outgoing:
                        written.append(output)
                        for chunk in iter(lambda:incoming.read(1024*1024),b''):
                            count+=len(chunk);actual_total+=len(chunk)
                            if count>entry.file_size or actual_total>byte_limit:raise EnvironmentError('source_limit','Actual expansion exceeds declared bounds')
                            h.update(chunk);outgoing.write(chunk)
                        outgoing.flush();os.fsync(outgoing.fileno())
                    if count!=entry.file_size:raise EnvironmentError('source_changed','Archive entry length mismatch')
                    output.chmod(0o700 if mode&0o111 else 0o600)
                    receipts.append({'path':name,'bytes':count,'sha256':h.hexdigest()})
                return {'source_sha256':source['sha256'],'files':receipts,'total_bytes':actual_total}
    except Exception:
        for output in reversed(written):output.unlink(missing_ok=True)
        # Caller owns removal of its staging directory, including empty parents.
        raise
