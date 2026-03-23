import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from src.db.connection import db

with db.get_session() as session:
    from sqlalchemy import text
    result = session.execute(text('UPDATE contracts SET listing_taken_in_work = true WHERE listing_taken_in_work IS NULL OR listing_taken_in_work = false'))
    print(f'Updated {result.rowcount} rows')
    session.commit()
print('Done')
