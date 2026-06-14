# Bug: typo in error message

The validation error message in `src/api/handler.py` reads "recieved" instead of
"received". Purely a spelling fix in a single string literal — no logic change.

Hypothesis: fix the typo.
