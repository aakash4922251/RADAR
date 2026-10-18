import io, sqlite3, zipfile
from pathlib import Path
from core.procurement_intelligence import init_intelligence_tables, process_bytes, process_zip

VNIT = Path('/mnt/data/radar_build/testdata/WONEWELECTRIFICATIONATNEWLYPROPOSEDFACULTYROOMSNEWMETALLURGICAL.pdf')

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
    pdf=VNIT.read_bytes()
    inner=io.BytesIO()
    with zipfile.ZipFile(inner,'w') as z: z.writestr('nested/work.pdf',pdf)
    outer=io.BytesIO()
    with zipfile.ZipFile(outer,'w') as z:
        z.writestr('one.pdf',pdf); z.writestr('bundle.zip',inner.getvalue())
    results=process_zip(conn,outer.getvalue(),'outer.zip')
    assert sum(r.status=='PROCESSED' for r in results)==1
    assert sum(r.status=='DUPLICATE' for r in results)==1
    assert conn.execute('select count(*) from radar_documents').fetchone()[0]==1
