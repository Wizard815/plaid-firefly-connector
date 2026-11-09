"""A bank's own CSV (the cheap way to bring in an account without a Plaid feed)."""
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from goblinbank import goblin, importers
from goblinbank.db import DB

H = {"X-Internal-Secret": "isec"}
CAPONE = "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n2026-11-01,2026-11-02,1234,KROGER #66,Groceries,54.20,\n2026-11-03,2026-11-04,1234,PAYMENT THANK YOU,Payment,,300.00\n2026-11-03,2026-11-04,1234,COFFEE,Dining,4.50,\n2026-11-03,2026-11-04,1234,COFFEE,Dining,4.50,\n"
DISCOVER = "Trans. Date,Post Date,Description,Amount,Category\n11/01/2026,11/02/2026,AMAZON MKTPLACE,23.99,Merchandise\n11/05/2026,11/05/2026,INTERNET PAYMENT - THANK YOU,-200.00,Payments and Credits\n"
CHECKING = "Account notes,,,\nDate,Description,Amount,Balance\n11/02/2026,PAYROLL ACME,2150.00,3100.00\n11/04/2026,SHEETZ,-31.20,3068.80\n"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"GOBLIN_NAME": "alice", "INTERNAL_SECRET": "isec", "FIELD_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(k, v)


def test_debit_credit_columns_and_duplicate_purchases():
    rows, bal = importers.parse_bank(CAPONE)
    assert [(r["date"], r["amount"]) for r in rows] == [("2026-11-02", -54.2), ("2026-11-04", 300.0), ("2026-11-04", -4.5), ("2026-11-04", -4.5)] and bal is None
    assert len({r["id"] for r in rows}) == 4                              # two identical coffees on one day are both kept


def test_credit_card_file_with_positive_purchases_needs_flip():
    rows, _ = importers.parse_bank(DISCOVER, flip=True)
    assert [r["amount"] for r in rows] == [-23.99, 200.0] and rows[0]["date"] == "2026-11-02"
    assert importers.parse_bank(DISCOVER, flip=False)[0][0]["amount"] == 23.99


def test_a_balance_column_sets_the_account_balance_and_reimport_adds_nothing():
    c = TestClient(goblin.create_app(db=DB(":memory:")))
    a = c.post("/api/accounts", headers=H, json={"name": "Axos Checking", "institution": "Axos", "kind": "cash", "balance": 0}).json()
    r = c.post(f"/import/bank-csv?account_id={a['id']}", headers=H, content=CHECKING).json()
    assert r == {"rows": 2, "added": 2, "balance": 3068.8}
    assert c.get(f"/api/accounts/{a['id']}", headers=H).json()["account"]["balance"] == 3068.8
    assert c.post(f"/import/bank-csv?account_id={a['id']}", headers=H, content=CHECKING).json()["added"] == 0
    # an overlapping later download only adds what is new
    more = CHECKING + "11/06/2026,NETFLIX,-15.49,3053.31\n"
    assert c.post(f"/import/bank-csv?account_id={a['id']}", headers=H, content=more).json()["added"] == 1


def test_rejects_unreadable_files_and_plaid_linked_accounts():
    c = TestClient(goblin.create_app(db=DB(":memory:")))
    a = c.post("/api/accounts", headers=H, json={"name": "X", "kind": "cash", "balance": 0}).json()
    assert c.post(f"/import/bank-csv?account_id={a['id']}", headers=H, content="foo,bar\n1,2\n").status_code == 422
    db = DB(":memory:")
    c2 = TestClient(goblin.create_app(db=db))
    db.conn.execute("INSERT INTO accounts(id,source,institution,name,kind,balance) VALUES('p1','plaid','Huntington','Checking','cash',1)"); db.conn.commit()
    assert c2.post("/import/bank-csv?account_id=p1", headers=H, content=CHECKING).status_code == 409
    assert c.post("/import/bank-csv?account_id=nope", headers=H, content=CHECKING).status_code == 404
