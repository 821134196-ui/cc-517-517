"""测试公共夹具：内存风格的临时 SQLite + 临时照片目录。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.meter import MockSoundMeter
from app.services import CampService


class CampTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "test.db"
        self.photo_dir = root / "readings"
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        self.svc = CampService(self.conn, MockSoundMeter(self.photo_dir))
        self._seed_basic()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _seed_basic(self):
        c = self.conn
        c.executemany(
            "INSERT INTO staff (name, role) VALUES (?,?)",
            [("值班员甲", "officer"), ("值班员乙", "officer"),
             ("巡查员甲", "patroller"), ("主管", "supervisor")])
        for code, x, y in [("A-01", 0, 0), ("A-02", 10, 0),
                           ("A-03", 20, 0), ("B-09", 200, 200),
                           ("C-01", 300, 0)]:
            c.execute("""INSERT INTO sites (code,name,zone,x,y,quiet_start,quiet_end,
                                            day_limit,night_limit)
                         VALUES (?,?,'A',?,?,'22:00','07:00',55,45)""",
                      (code, code, x, y))
        c.executemany(
            "INSERT INTO adjacency (a_code,b_code,distance_m) VALUES (?,?,8)",
            [("A-01", "A-02"), ("A-02", "A-03")])
        guests = [
            ("投诉人甲", "13900001", "tok-a", "A-01"),
            ("投诉人乙", "13900002", "tok-b", "A-03"),
            ("被投诉人", "13900003", "tok-z", "A-02"),
            ("远营位住客", "13900004", "tok-f", "B-09"),
        ]
        for name, phone, token, site in guests:
            cur = c.execute("INSERT INTO guests (name,contact,anon_token) VALUES (?,?,?)",
                            (name, phone, token))
            c.execute("INSERT INTO occupancies (guest_id,site_code,check_in,active) "
                      "VALUES (?,?,'2026-09-30 12:00:00',1)", (cur.lastrowid, site))
        c.commit()
