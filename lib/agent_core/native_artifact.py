"""Native archive inspection and exact sealed-image self checks.

Entry inspect_archive/seal_archive. The generated artifact contract owns bounds.
A private compressed snapshot prevents path/content replacement between pin and
member checks. Members stream into digests; only executable/metadata survive.
No install tree, download, publisher authentication or deployment is performed.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import stat
import subprocess
import tarfile
import tempfile
import time
import zlib

from .native_content_contract import NATIVE_ARTIFACT_SPEC as SPEC,NATIVE_STDIO_CONTRACT
from .native_token_count_contract import TOKEN_COUNT_SPEC
from .native_html_projection_contract import SPEC as HTML_SPEC
from lib.orchestration.native_flow_contract import SPEC as FLOW_SPEC
from lib.native_binary.generated_checkpoint_storage_contract import SPEC as CHECKPOINT_STORAGE_SPEC
from .native_binary import NativeConfigurationError,_memfd,_ADD_SEALS,_ALL_SEALS


def _refuse(message):
    return NativeConfigurationError('native archive '+message)


def _write_all(fd,chunk):
    offset=0
    while offset<len(chunk):
        count=os.write(fd,chunk[offset:])
        if count<=0:raise _refuse('image write failed')
        offset+=count


class _BoundedTarInfo(tarfile.TarInfo):
    @classmethod
    def frombuf(cls,buf,encoding,errors):
        entry=super().frombuf(buf,encoding,errors)
        if entry.type==tarfile.GNUTYPE_SPARSE:raise _refuse('sparse member invalid')
        if entry.size>SPEC['maximum_unpacked_bytes'] or (entry.type in (tarfile.XHDTYPE,tarfile.XGLTYPE,tarfile.GNUTYPE_LONGNAME,tarfile.GNUTYPE_LONGLINK) and entry.size>SPEC['maximum_metadata_bytes']):
            raise _refuse('metadata/member byte bound exceeded')
        return entry


_ARCHIVE_CHUNK_BYTES = 65536


class _BoundedGzipReader:
    """Borrow compressed snapshot bytes; bound decoded output before tar parsing."""
    def __init__(self, source):
        self.source=source
        self.decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
        self.pending=b''
        self.decoded=0
        self.failure=None
        # Member bytes, aggregate extended metadata, headers/padding and the
        # writer's final tar record all have independent canonical bounds.
        self.maximum=(SPEC['maximum_unpacked_bytes']+SPEC['maximum_metadata_bytes']
            +(3*SPEC['maximum_members']+2)*tarfile.BLOCKSIZE+tarfile.RECORDSIZE)

    def refuse(self,message):
        self.failure=message
        raise _refuse(message)

    def read(self, size):
        if self.failure is not None:raise _refuse(self.failure)
        if type(size) is not int or size<0:raise _refuse('decoded read bound invalid')
        remaining=min(size,_ARCHIVE_CHUNK_BYTES)
        chunks=[]
        while remaining and not self.decoder.eof:
            raw=self.pending or self.source.read(_ARCHIVE_CHUNK_BYTES)
            if not raw:self.refuse('compressed stream truncated')
            try:
                chunk=self.decoder.decompress(raw,min(remaining,self.maximum-self.decoded+1))
            except zlib.error:self.refuse('compressed stream invalid')
            self.pending=self.decoder.unconsumed_tail
            self.decoded+=len(chunk)
            if self.decoded>self.maximum:self.refuse('decoded archive byte bound exceeded')
            chunks.append(chunk);remaining-=len(chunk)
        return b''.join(chunks)

    def finish(self):
        # Tar can finish before the gzip trailer. Complete bounded decoding so
        # CRC/size/truncation refusal precedes any executable self check.
        while self.read(_ARCHIVE_CHUNK_BYTES):pass


def _self_check(image,command,directory,timeout,output_bytes):
    process=subprocess.Popen([f'/proc/self/fd/{image.fileno()}',command],
        pass_fds=(image.fileno(),),cwd=directory,env={'PATH':str(directory/'empty-path')},
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    output=bytearray();total=0;deadline=time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout,process.stderr):
                os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ)
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:raise _refuse('self-check timed out')
                for key,_ in selector.select(remaining):
                    chunk=os.read(key.fileobj.fileno(),min(65536,output_bytes-total+1))
                    if not chunk:selector.unregister(key.fileobj);continue
                    total+=len(chunk)
                    if total>output_bytes:raise _refuse('self-check output byte bound exceeded')
                    if key.fileobj is process.stdout:output.extend(chunk)
            if process.wait(timeout=max(.001,deadline-time.monotonic()))!=0:raise _refuse('self-check failed')
        return json.loads(output)
    except (subprocess.TimeoutExpired,ValueError) as error:
        if isinstance(error,NativeConfigurationError):raise
        raise _refuse('self-check output invalid') from None
    finally:
        if process.poll() is None:process.kill()
        process.wait()
        process.stdout.close();process.stderr.close()


def inspect_archive(path:Path,*,archive_sha256=None,application_version=None,binary_bytes=None,
                    seal_image=False,verify_run=False,timeout=30,output_bytes=65536,image_member='bin/tofu-native'):
    """Verify one immutable private snapshot; return manifest and owned image.

    Development inspection may omit a pin. Runtime callers must supply a trusted
    compressed-archive digest and expected version through seal_archive.
    """
    extensions=((SPEC['optional_token_count'],TOKEN_COUNT_SPEC,'token-count'),
                (SPEC['optional_flow'],FLOW_SPEC,'Flow'),
                (SPEC['optional_html_projection'],HTML_SPEC,'HTML projection'),
                (SPEC['optional_checkpoint_storage'],CHECKPOINT_STORAGE_SPEC,'SQL checkpoint'))
    library_members={extension['member'] for extension,_,_ in extensions}
    contract_members={extension['contract_member'] for extension,_,_ in extensions}
    if image_member not in {'bin/tofu-native',*library_members}:raise _refuse('image member unsupported')
    if verify_run and image_member!='bin/tofu-native':raise _refuse('self-check requires executable image')
    if verify_run and not seal_image:raise _refuse('self-check requires sealed image')
    if archive_sha256 is not None and (not isinstance(archive_sha256,str) or len(archive_sha256)!=64 or any(ch not in '0123456789abcdef' for ch in archive_sha256)):
        raise _refuse('pin must be SHA-256')
    limit=SPEC['maximum_unpacked_bytes'];binary_bytes=limit if binary_bytes is None else binary_bytes
    descriptor=None;image=None
    try:
        source_fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(source_fd,'rb') as source,tempfile.TemporaryFile() as snapshot:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>limit:raise _refuse('source byte/type bound exceeded')
            digest=hashlib.sha256();compressed=0
            while chunk:=source.read(65536):
                compressed+=len(chunk)
                if compressed>limit:raise _refuse('compressed byte bound exceeded')
                digest.update(chunk);snapshot.write(chunk)
            if archive_sha256 is not None and digest.hexdigest()!=archive_sha256:raise _refuse('checksum differs')
            snapshot.seek(0);records={};metadata={};total=0
            if seal_image:descriptor=_memfd()
            class BudgetedTarInfo(_BoundedTarInfo):
                headers=0
                metadata_bytes=0
                @classmethod
                def frombuf(cls,buf,encoding,errors):
                    entry=super().frombuf(buf,encoding,errors)
                    cls.headers+=1
                    if entry.type in (tarfile.XHDTYPE,tarfile.XGLTYPE,tarfile.GNUTYPE_LONGNAME,tarfile.GNUTYPE_LONGLINK):
                        cls.metadata_bytes+=entry.size
                    if cls.headers>2*SPEC['maximum_members']+2 or cls.metadata_bytes>SPEC['maximum_metadata_bytes']:
                        raise _refuse('metadata/header aggregate bound exceeded')
                    return entry
            decompressed=_BoundedGzipReader(snapshot)
            with tarfile.open(fileobj=decompressed,mode='r|',bufsize=_ARCHIVE_CHUNK_BYTES,tarinfo=BudgetedTarInfo) as archive:
                for entry in archive:
                    name=entry.name
                    if (not entry.isfile() or entry.sparse is not None or name in records or name.startswith('/')
                            or any(part in {'','.','..'} for part in name.split('/'))
                            or entry.size<0 or len(records)>=SPEC['maximum_members']):raise _refuse('member invalid')
                    total+=entry.size
                    if total>limit:raise _refuse('unpacked byte bound exceeded')
                    if name==image_member and entry.size>binary_bytes:raise _refuse('executable byte budget exceeded')
                    retain=name in {'manifest.json','contracts/native_agent_stdio_v1.schema.json','contracts/native_runtime_artifact_v1.json',*contract_members}
                    if retain and entry.size>SPEC['maximum_metadata_bytes']:raise _refuse('metadata byte bound exceeded')
                    reader=archive.extractfile(entry)
                    if reader is None:raise _refuse('member missing')
                    hasher=hashlib.sha256();length=0;body=bytearray()
                    while chunk:=reader.read(65536):
                        if name in {'bin/tofu-native',*library_members} and length==0 and not chunk.startswith(b'\x7fELF'):raise _refuse('executable is not ELF')
                        length+=len(chunk);hasher.update(chunk)
                        if retain:body.extend(chunk)
                        if name==image_member and descriptor is not None:_write_all(descriptor,chunk)
                    if length!=entry.size:raise _refuse('member truncated')
                    records[name]={'sha256':hasher.hexdigest(),'bytes':length}
                    if retain:metadata[name]=json.loads(body)
            decompressed.finish()
            manifest=metadata.get('manifest.json')
            if not isinstance(manifest,dict) or manifest.get('format')!=SPEC['archive_format']:raise _refuse('manifest invalid')
            members=manifest.get('files')
            if not isinstance(members,dict) or set(records)!={'manifest.json',*members}:raise _refuse('member inventory differs')
            for name,value in members.items():
                if (not isinstance(value,dict) or type(value.get('bytes')) is not int
                        or value!=records[name]):raise _refuse('member checksum differs')
            if not set(SPEC['required_members']).issubset(records) or records['bin/tofu-native']['bytes']<4:raise _refuse('required member missing')
            declared_libraries=set()
            for extension,expected,label in extensions:
                member=extension['member'];contract_member=extension['contract_member']
                if contract_member not in records:continue
                if (member not in records or records[member]['bytes']<4
                        or metadata.get(contract_member)!=expected):
                    raise _refuse(label+' extension differs')
                declared_libraries.add(member)
            if library_members.intersection(records)!=declared_libraries:
                raise _refuse('library extension contract missing')
            if seal_image and image_member not in records:raise _refuse('selected image member missing')
            build=manifest.get('build');target=manifest.get('target')
            if (not isinstance(build,dict) or build.get('format')!=SPEC['build_format']
                    or not isinstance(target,str) or target not in SPEC['supported_targets'] or build.get('target_os')!='linux'
                    or not isinstance(build.get('application_version'),str) or not build['application_version']
                    or not isinstance(metadata.get('contracts/native_agent_stdio_v1.schema.json'),dict)
                    or not isinstance(metadata.get('contracts/native_runtime_artifact_v1.json'),dict)
                    or build.get('target_arch')!=target.split('-')[0]
                    or build.get('stdio_contract')!=NATIVE_STDIO_CONTRACT
                    or metadata['contracts/native_agent_stdio_v1.schema.json'].get('$id')!=NATIVE_STDIO_CONTRACT
                    or metadata['contracts/native_runtime_artifact_v1.json'].get('contract_version')!=SPEC['contract_version']
                    or (application_version is not None and build.get('application_version')!=application_version)):
                raise _refuse('version/protocol/target differs')
            if descriptor is not None:
                fcntl.fcntl(descriptor,_ADD_SEALS,_ALL_SEALS)
                image=os.fdopen(descriptor,'rb',buffering=0);descriptor=None
            if verify_run:
                host={'x86_64':'x86_64','aarch64':'aarch64'}.get(platform.machine())
                if platform.system()!='Linux' or build['target_arch']!=host:raise _refuse('target does not match host')
                with tempfile.TemporaryDirectory(prefix='tofu-native-check-') as directory:
                    results={command:_self_check(image,command,Path(directory),timeout,output_bytes) for command in SPEC['identity_commands']}
                if (any(not isinstance(value,dict) for value in results.values()) or results['build-info']!=build or results['check-session'].get('invariants_satisfied') is not True
                        or results['check-tool-gateway'].get('default_deny') is not True):raise _refuse('identity/runtime gate failed')
            return manifest,image
    except BaseException:
        if image is not None:image.close()
        if descriptor is not None:os.close(descriptor)
        raise


def seal_executable(path, executable_sha256, application_version, binary_bytes, timeout, output_bytes):
    """Admit installed bytes with their own trusted pin and complete runtime checks.

    The installation boundary validates the distribution. A caller must retain
    that installation's executable digest; hashing an untrusted current file is
    not a substitute for the pin. No mutable pathname is ever executed.
    """
    from .native_binary import _seal
    if not isinstance(application_version, str) or not application_version:
        raise NativeConfigurationError('native executable expected version required')
    image = _seal(path, executable_sha256, binary_bytes)
    try:
        with tempfile.TemporaryDirectory(prefix='tofu-native-check-') as directory:
            results = {command: _self_check(image, command, Path(directory), timeout, output_bytes)
                       for command in SPEC['identity_commands']}
        build = results.get('build-info')
        host = {'x86_64': 'x86_64', 'aarch64': 'aarch64'}.get(platform.machine())
        if (platform.system() != 'Linux' or host is None
                or not isinstance(build, dict) or build.get('format') != SPEC['build_format']
                or build.get('application_version') != application_version
                or build.get('stdio_contract') != NATIVE_STDIO_CONTRACT
                or build.get('target_os') != 'linux' or build.get('target_arch') != host
                or any(not isinstance(value, dict) for value in results.values())
                or results['check-session'].get('invariants_satisfied') is not True
                or results['check-tool-gateway'].get('default_deny') is not True):
            raise NativeConfigurationError('native executable version/target/runtime gate failed')
        image.seek(0)
        return image
    except BaseException:
        image.close()
        raise


def seal_archive(path,archive_sha256,application_version,binary_bytes,timeout,output_bytes):
    _,image=inspect_archive(path,archive_sha256=archive_sha256,application_version=application_version,
        binary_bytes=binary_bytes,seal_image=True,verify_run=True,timeout=timeout,output_bytes=output_bytes)
    return image


def _inspect_library_archive(path,archive_sha256,application_version,binary_bytes,extension):
    """Return checked manifest and pinned sealed library; caller owns descriptor.

    Inspection does not load or execute the library. Old archives without the
    optional extension remain valid runtime archives and refuse this selection.
    """
    if archive_sha256 is None:raise _refuse('trusted checksum required')
    if not isinstance(application_version,str) or not application_version:raise _refuse('expected version required')
    if type(binary_bytes) is not int or not 4<=binary_bytes<=SPEC['maximum_unpacked_bytes']:
        raise _refuse('library byte budget invalid')
    manifest,image=inspect_archive(path,archive_sha256=archive_sha256,
        application_version=application_version,binary_bytes=binary_bytes,
        seal_image=True,image_member=extension['member'])
    try:
        if extension['contract_member'] not in manifest['files']:
            raise _refuse('selected library extension missing')
        host={'x86_64':'x86_64','aarch64':'aarch64'}.get(platform.machine())
        if platform.system()!='Linux' or manifest['build']['target_arch']!=host:
            raise _refuse('target does not match host')
        image.seek(0)
        return manifest,image
    except BaseException:
        image.close()
        raise


def seal_token_count_archive(path,archive_sha256,application_version,binary_bytes):
    """Return a pinned sealed token library; caller owns its descriptor."""
    return _inspect_library_archive(path,archive_sha256,application_version,binary_bytes,SPEC['optional_token_count'])[1]


def seal_flow_archive(path,archive_sha256,application_version,binary_bytes):
    """Return a pinned sealed Flow library without loading or executing code."""
    return _inspect_library_archive(path,archive_sha256,application_version,binary_bytes,SPEC['optional_flow'])[1]


def seal_html_projection_archive(path,archive_sha256,application_version,binary_bytes):
    """Return pinned sealed HTML projection bytes; caller owns the descriptor."""
    return _inspect_library_archive(path,archive_sha256,application_version,binary_bytes,SPEC['optional_html_projection'])[1]


def seal_checkpoint_storage_archive(path,archive_sha256,application_version,binary_bytes):
    """Return verified SQL checkpoint library bytes without loading a driver."""
    return _inspect_library_archive(path,archive_sha256,application_version,binary_bytes,SPEC['optional_checkpoint_storage'])[1]
