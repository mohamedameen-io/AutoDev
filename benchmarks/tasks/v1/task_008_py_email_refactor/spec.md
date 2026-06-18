# task_008_py_email_refactor — de-duplicate _valid_email (refactor)

The function `_valid_email(addr)` is **copy-pasted verbatim** in three modules:
`users.py`, `orders.py`, and `contacts.py`. This is a maintenance hazard.

**Refactor** the code so `_valid_email` is defined exactly once in a shared
module (e.g. `validation.py`) and imported from all three call sites. Do not
change behaviour — every input must validate exactly as it does today.

## Constraints
- The public functions (`register_user`, `order_receipt_email`, `add_contact`)
  keep their signatures and behaviour.
- After the refactor there must be exactly ONE definition of `_valid_email`.

## How to verify
Run `bash test_command.sh` from this repo root. It must exit `0` (behaviour
preserved). Note: the benchmark separately requires a real structural change —
a no-op will not count as a refactor.
