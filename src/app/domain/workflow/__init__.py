"""Workflow orchestration vocabulary, with no orchestration library in sight.

§11.2's execution contract is data. Keeping its enums here -- rather than in the
graph package or in the ORM -- is what lets the plan be tested without a
database and swapped onto a different runtime without renaming anything.
"""
