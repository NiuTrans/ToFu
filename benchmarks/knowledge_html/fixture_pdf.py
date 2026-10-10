"""Reproducible four-page text PDF for native ingest comparisons."""
import argparse
from pathlib import Path


def create():
    import pymupdf
    document=pymupdf.open()
    try:
        for index in range(4):
            page=document.new_page()
            for line in range(12):
                page.insert_text((60,70+line*22),f'Page {index} row {line}: immutable native ingestion, provenance and exact parser evidence.')
        return document.tobytes(no_new_id=True)
    finally:document.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.parse_args().output.write_bytes(create())
