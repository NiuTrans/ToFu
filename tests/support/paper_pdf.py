"""Small real PDFs with exact xref offsets, independent of production parsers."""


def paper_objects() -> list[bytes]:
    stream = b'BT /F1 12 Tf 32 220 Td (Paper build fixture) Tj ET'
    return [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']


def pdf_from_objects(objects: list[bytes], *, padding: int = 0) -> bytes:
    body = bytearray(b'%PDF-1.4\n%' + b'x' * padding + b'\n')
    offsets = []
    for number, content in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(str(number).encode() + b' 0 obj\n' + content + b'\nendobj\n')
    start = len(body)
    count = len(objects) + 1
    body.extend(f'xref\n0 {count}\n0000000000 65535 f \n'.encode())
    body.extend(b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets))
    body.extend(f'trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    return bytes(body)


def paper_pdf(padding: int = 150000) -> bytes:
    return pdf_from_objects(paper_objects(), padding=padding)
