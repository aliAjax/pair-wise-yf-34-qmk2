"""冲突协商单存储层：表结构与 SQL 读写，不做任何业务判定。

判定逻辑见 negotiation_rules.py，业务编排见 negotiation_service.py。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS negotiation_tickets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
    status TEXT NOT NULL DEFAULT 'open',
    close_kind TEXT,
    close_reason TEXT,
    created_by TEXT NOT NULL,
    created_role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_negotiation_open_per_plan
    ON negotiation_tickets(primary_plan_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS negotiation_parties(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES negotiation_tickets(id),
    plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
    operator_id TEXT NOT NULL,
    role_in_ticket TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    confirmed_revision INTEGER,
    confirmed_by TEXT,
    confirmed_at TEXT,
    party_status TEXT NOT NULL DEFAULT 'pending',
    exit_reason TEXT,
    UNIQUE(ticket_id,plan_id)
);
"""


class NegotiationStore:
    """协商单与参与方行的存取；所有方法接收外层事务连接，不自行提交。"""

    @staticmethod
    def create_ticket(conn: sqlite3.Connection, primary_plan_id: int, created_by: str, created_role: str, created_at: str) -> int:
        cur = conn.execute("INSERT INTO negotiation_tickets(primary_plan_id,created_by,created_role,created_at) VALUES(?,?,?,?)",
                           (primary_plan_id, created_by, created_role, created_at))
        return int(cur.lastrowid)

    @staticmethod
    def add_party(conn: sqlite3.Connection, ticket_id: int, plan_id: int, operator_id: str, role_in_ticket: str, base_revision: int) -> None:
        conn.execute("INSERT INTO negotiation_parties(ticket_id,plan_id,operator_id,role_in_ticket,base_revision) VALUES(?,?,?,?,?)",
                     (ticket_id, plan_id, operator_id, role_in_ticket, base_revision))

    @staticmethod
    def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM negotiation_tickets WHERE id=?", (ticket_id,)).fetchone()

    @staticmethod
    def get_open_ticket_for_plan(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM negotiation_tickets WHERE primary_plan_id=? AND status='open'", (plan_id,)).fetchone()

    @staticmethod
    def list_parties(conn: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM negotiation_parties WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()

    @staticmethod
    def get_party(conn: sqlite3.Connection, ticket_id: int, plan_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM negotiation_parties WHERE ticket_id=? AND plan_id=?", (ticket_id, plan_id)).fetchone()

    @staticmethod
    def confirm_party(conn: sqlite3.Connection, party_id: int, revision: int, actor: str, when: str) -> None:
        conn.execute("UPDATE negotiation_parties SET party_status='confirmed',confirmed_revision=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                     (revision, actor, when, party_id))

    @staticmethod
    def set_party_status(conn: sqlite3.Connection, party_id: int, status: str, exit_reason: str | None = None) -> None:
        conn.execute("UPDATE negotiation_parties SET party_status=?,exit_reason=? WHERE id=?", (status, exit_reason, party_id))

    @staticmethod
    def close_ticket(conn: sqlite3.Connection, ticket_id: int, status: str, close_kind: str, close_reason: str, when: str) -> None:
        conn.execute("UPDATE negotiation_tickets SET status=?,close_kind=?,close_reason=?,closed_at=? WHERE id=?",
                     (status, close_kind, close_reason, when, ticket_id))

    @staticmethod
    def list_tickets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM negotiation_tickets ORDER BY id DESC").fetchall()

    @staticmethod
    def list_open_tickets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM negotiation_tickets WHERE status='open' ORDER BY id").fetchall()

    @staticmethod
    def tickets_for_operator(conn: sqlite3.Connection, operator_id: str) -> list[sqlite3.Row]:
        return conn.execute("""SELECT DISTINCT t.* FROM negotiation_tickets t
                               JOIN negotiation_parties p ON p.ticket_id=t.id
                               WHERE p.operator_id=? ORDER BY t.id DESC""", (operator_id,)).fetchall()

    @staticmethod
    def open_related_tickets_for_plan(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
        """计划作为相关方参与、且仍未结束的协商单。"""
        return conn.execute("""SELECT t.* FROM negotiation_tickets t
                               JOIN negotiation_parties p ON p.ticket_id=t.id
                               WHERE p.plan_id=? AND p.role_in_ticket='related' AND t.status='open'""", (plan_id,)).fetchall()
