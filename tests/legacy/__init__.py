"""Frozen copies of code that an earlier version ran, used only by the compatibility tests.

personal_auth_previous.py is the previous release's personal_auth.py (the same bytes as the previous version) with the four em dashes of its comments and docstring replaced by a hyphen, so that tests/test_hygiene.py
can scan this directory. Nothing else is different. Do not edit it: tests/test_personal_auth_tokens.py pins its digest
and proves that only those four lines differ. It is what a rollback would put back, so the tests use it to prove that
the state file the current code writes can still be loaded by it, and that the file it writes can be loaded by the
current code.
"""
