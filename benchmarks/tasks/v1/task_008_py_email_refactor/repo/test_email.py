"""Behaviour tests for email validation across modules.

These pass BEFORE and AFTER the refactor (behaviour is preserved). The
benchmark's structural-change guard (meta task_type=refactor) is what rejects a
vacuous no-op "refactor".
"""

from contacts import add_contact
from orders import order_receipt_email
from users import register_user


def test_valid_emails():
    assert register_user("A", "a@example.com")["email_ok"] is True
    assert order_receipt_email("b@example.org") is True
    assert add_contact("C", "c@x.co")["valid"] is True


def test_invalid_emails():
    for bad in ["", "no-at", "a@@b.com", "a@nodot", "@example.com", "x@"]:
        assert register_user("U", bad)["email_ok"] is False
        assert order_receipt_email(bad) is False
        assert add_contact("U", bad)["valid"] is False
