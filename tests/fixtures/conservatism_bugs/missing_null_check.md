# Bug: missing null check

`src/profile.py:render` raises `AttributeError` when `user.profile` is `None`
(users created before the profile migration). Add a guard that returns an empty
profile view when `profile is None`.

Hypothesis: add a null check.
