"""Batch-process thousands of procurement documents without opening Streamlit."""
from __future__ import annotations
import argparse
from db.database import get_connection, init_db
from core.procurement_intelligence import process_directory

p=argparse.ArgumentParser(description='Government Procurement Opportunity Radar batch importer')
p.add_argument('--inbox', default='data/inbox')
a=p.parse_args()
init_db(); conn=get_connection()
results=process_directory(conn,a.inbox,recursive=True)
print(f'Processed: {sum(r.status=="PROCESSED" for r in results)}')
print(f'Duplicates: {sum(r.status=="DUPLICATE" for r in results)}')
print(f'Failed: {sum(r.status=="FAILED" for r in results)}')
for r in results:
    print(f'{r.status:9} {r.document_type:20} {r.filename}')
