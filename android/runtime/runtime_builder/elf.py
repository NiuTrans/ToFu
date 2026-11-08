"""ELF identification and LOAD-segment alignment (design §8, 16 KB pages).

Entry point: ``inspect_elf(path)`` returns the machine, class, type,
interpreter and every PT_LOAD ``p_align`` of an ELF file, or ``None`` for any
other file. Pure stdlib; it reads only the ELF and program headers.
"""
from __future__ import annotations

import struct

ELF_MAGIC = b'\x7fELF'
PT_LOAD = 1
PT_INTERP = 3
# e_machine values (System V ABI, "Machine" table).
MACHINES = {62: 'x86_64', 183: 'aarch64', 40: 'arm', 3: 'i386', 243: 'riscv'}
ELF_TYPES = {1: 'rel', 2: 'exec', 3: 'dyn', 4: 'core'}
# Android requires LOAD segments aligned to the 16 KB page size
# (https://developer.android.com/guide/practices/page-sizes).
PAGE_16K = 0x4000


def is_elf(head: bytes) -> bool:
    return head[:4] == ELF_MAGIC


def inspect_elf(path) -> dict | None:
    with open(path, 'rb') as handle:
        ident = handle.read(64)
        if len(ident) < 52 or not is_elf(ident):
            return None
        wide = ident[4] == 2
        endian = '<' if ident[5] == 1 else '>'
        e_type, e_machine = struct.unpack_from(endian + 'HH', ident, 16)
        if wide:
            phoff = struct.unpack_from(endian + 'Q', ident, 32)[0]
            phentsize, phnum = struct.unpack_from(endian + 'HH', ident, 54)
        else:
            phoff = struct.unpack_from(endian + 'I', ident, 28)[0]
            phentsize, phnum = struct.unpack_from(endian + 'HH', ident, 42)
        handle.seek(phoff)
        table = handle.read(phentsize * phnum)
        aligns, interpreter = [], None
        for index in range(phnum):
            base = index * phentsize
            if len(table) < base + phentsize:
                break
            p_type = struct.unpack_from(endian + 'I', table, base)[0]
            if wide:
                p_offset = struct.unpack_from(endian + 'Q', table, base + 8)[0]
                p_filesz = struct.unpack_from(endian + 'Q', table, base + 32)[0]
                p_align = struct.unpack_from(endian + 'Q', table, base + 48)[0]
            else:
                p_offset = struct.unpack_from(endian + 'I', table, base + 4)[0]
                p_filesz = struct.unpack_from(endian + 'I', table, base + 16)[0]
                p_align = struct.unpack_from(endian + 'I', table, base + 28)[0]
            if p_type == PT_LOAD:
                aligns.append(p_align)
            elif p_type == PT_INTERP:
                handle.seek(p_offset)
                interpreter = handle.read(p_filesz).rstrip(b'\0').decode('utf-8', 'replace')
    return {
        'machine': MACHINES.get(e_machine, f'e_machine={e_machine}'),
        'class': 64 if wide else 32,
        'type': ELF_TYPES.get(e_type, f'e_type={e_type}'),
        'interpreter': interpreter,
        'load_aligns': aligns,
        'min_load_align': min(aligns) if aligns else None,
    }
