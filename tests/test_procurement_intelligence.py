import io, sqlite3, zipfile
from pathlib import Path

import pytest

from core.ingestion import ocr_available
from core.procurement_intelligence import init_intelligence_tables, process_bytes, process_zip

# Real VNIT work order used as a regression sample. It is a scanned image, so
# the regression needs OCR (Tesseract) as well as the file.
VNIT = Path(__file__).parent / 'fixtures' / 'WONEWELECTRIFICATIONATNEWLYPROPOSEDFACULTYROOMSNEWMETALLURGICAL.pdf'

@pytest.mark.skipif(not VNIT.exists() or not ocr_available(),
                    reason=f"needs {VNIT.name} and OCR (Tesseract) installed")
def test_vnit_work_order_regression():
    conn=sqlite3.connect(':memory:')
    r=process_bytes(conn,VNIT.read_bytes(),VNIT.name)
    facts={f.fact_type:f.value for f in r.facts}
    assert r.document_type == 'WORK_ORDER'
    assert facts['work_order_date'] == '2026-09-16'
    assert facts['completion_period'] == '20 days'
    assert facts['completion_deadline'] == '2026-10-06'
    assert facts['warranty_period'] == '01 Year'
    assert facts['vendor'] == 'Bhagyshree Electrical'
    assert facts['organisation'].startswith('Visvesvaraya National Institute of Technology')

def test_zip_nested_and_deduplication():
    conn=sqlite3.connect(':memory:')
    doc=b'Work Order No. 12/2025 dated 16/09/2025. Completion period: 20 days.'
    inner=io.BytesIO()
    with zipfile.ZipFile(inner,'w') as z: z.writestr('nested/work.txt',doc)
    outer=io.BytesIO()
    with zipfile.ZipFile(outer,'w') as z:
        z.writestr('one.txt',doc); z.writestr('bundle.zip',inner.getvalue())
    results=process_zip(conn,outer.getvalue(),'outer.zip')
    assert sum(r.status=='PROCESSED' for r in results)==1
    assert sum(r.status=='DUPLICATE' for r in results)==1
    assert conn.execute('select count(*) from radar_documents').fetchone()[0]==1
